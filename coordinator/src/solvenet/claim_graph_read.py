"""Indexed, snapshot-consistent neighborhood inspection with explicit omissions."""

import json
from contextlib import nullcontext

from .group_state import _number, _require, _require_group

MAX_READ_NODES = 32
MAX_READ_ITEMS = 64
MAX_READ_DEPTH = 3
MAX_READ_BYTES = 256 * 1024


class ClaimGraphRead:
    def group_claim_neighborhood(self, group_id, claim_id=None, *, depth=1,
                                 max_nodes=16, max_items=32, max_bytes=65536,
                                 verifier_identity=None, _db=None, _outgoing=False, _recent=False):
        """Traverse planning links in both directions; no proof dependency inference.

        Query count depends on bounded depth, not on the number of returned nodes.
        Lists are insertion ordered; omissions include row and encoded-byte caps.
        """
        _number(max_nodes, 'max_nodes', MAX_READ_NODES)
        _number(max_items, 'max_items', MAX_READ_ITEMS)
        _number(max_bytes, 'max_bytes', MAX_READ_BYTES)
        if type(depth) is not int or not 0 <= depth <= MAX_READ_DEPTH:
            raise ValueError('Invalid graph read depth')
        with (self.connect() if _db is None else nullcontext(_db)) as db:
            if _db is None:
                db.execute('BEGIN')
            _require_group(db, group_id)
            graph = dict(db.execute('SELECT * FROM group_graphs WHERE group_id=?',
                                    (group_id,)).fetchone())
            result = {**graph, 'focus_id': claim_id or graph['root_id'],
                      'claims': [], 'relationships': [], 'publications': [], 'reviews': [],
                      'tasks': [], 'messages': [], 'artifacts': [], 'outcomes': [],
                      'omitted': {name: 0 for name in ('claims', 'relationships', 'publications',
                                                     'reviews', 'tasks', 'messages', 'artifacts', 'outcomes')}}
            if result['focus_id'] is None:
                result['omitted']['traversal_truncated'] = False
                if len(json.dumps(result).encode()) > max_bytes:
                    raise ValueError('max_bytes too small for graph metadata')
                return result
            _require(db, 'group_claims', group_id, result['focus_id'])
            nodes = [result['focus_id']]
            frontier = nodes[:]
            truncated = False
            omitted_nodes = 0
            for _ in range(depth):
                if not frontier:
                    break
                marks = ','.join('?' * len(frontier))
                seen = ','.join('?' * len(nodes))
                reverse = '' if _outgoing else f'''UNION SELECT from_id AS id FROM claim_relationships
                    WHERE group_id=? AND to_id IN ({marks})'''
                rows = db.execute(f'''SELECT id,count(*) OVER () AS total FROM (
                    SELECT DISTINCT to_id AS id FROM claim_relationships WHERE group_id=? AND from_id IN ({marks})
                    {reverse})
                    WHERE id NOT IN ({seen}) ORDER BY id LIMIT ?''',
                    (group_id, *frontier, *((group_id, *frontier) if not _outgoing else ()),
                     *nodes, max_nodes - len(nodes) + 1)).fetchall()
                room = max_nodes - len(nodes)
                truncated |= len(rows) > room
                omitted_nodes += max(0, (rows[0]['total'] if rows else 0) - room)
                frontier = [r['id'] for r in rows[:room]]
                nodes.extend(frontier)
                if truncated:
                    break
            result['omitted']['traversal_truncated'] = truncated
            marks = ','.join('?' * len(nodes))
            params = (group_id, *nodes)

            def collect(name, query, values, cap=max_items):
                if _recent and name == 'messages':
                    query = query.replace('ORDER BY cm.rowid', '''ORDER BY
                        CASE WHEN m.review_status='pending' THEN 1 ELSE 0 END,
                        m.reviewed_revision IS NULL,m.reviewed_revision DESC,cm.rowid''')
                if _recent and name in ('messages', 'publications', 'artifacts', 'outcomes'):
                    query += ' DESC'
                count = db.execute(f'SELECT count(*) FROM ({query})', values).fetchone()[0]
                rows = [dict(r) for r in db.execute(query + ' LIMIT ?', (*values, cap))]
                result['omitted'][name] = count - len(rows)
                result[name] = rows

            collect('claims', f'SELECT * FROM group_claims WHERE group_id=? AND id IN ({marks}) ORDER BY rowid',
                    params, max_nodes)
            result['omitted']['claims'] += omitted_nodes
            collect('publications', f'SELECT * FROM claim_publications WHERE group_id=? AND claim_id IN ({marks}) ORDER BY rowid', params)
            collect('relationships', f'''SELECT * FROM claim_relationships WHERE group_id=?
                AND from_id IN ({marks}) AND to_id IN ({marks}) ORDER BY rowid''', (group_id, *nodes, *nodes))
            # Count reviews across the entire induced neighborhood, even when
            # the relationship row cap excludes their edge. Return only reviews
            # of returned edges, and account for the others as omissions.
            review_query = f'''SELECT v.* FROM claim_relationship_reviews v
                JOIN claim_relationships r ON r.group_id=v.group_id AND r.id=v.relationship_id
                WHERE r.group_id=? AND r.from_id IN ({marks}) AND r.to_id IN ({marks})'''
            review_params = (group_id, *nodes, *nodes)
            review_count = db.execute(f'SELECT count(*) FROM ({review_query})', review_params).fetchone()[0]
            edges = [r['id'] for r in result['relationships']]
            if edges:
                edge_marks = ','.join('?' * len(edges))
                current = (''' AND v.rowid=(SELECT max(v2.rowid) FROM claim_relationship_reviews v2
                    WHERE v2.group_id=v.group_id AND v2.relationship_id=v.relationship_id)'''
                    if _recent else '')
                result['reviews'] = [dict(r) for r in db.execute(
                    review_query + f' AND r.id IN ({edge_marks})' + current + ' ORDER BY v.rowid ' +
                    ('DESC ' if _recent else '') + 'LIMIT ?',
                    (*review_params, *edges, max_items))]
            result['omitted']['reviews'] = review_count - len(result['reviews'])
            collect('tasks', f'''SELECT ct.*,t.owner_id,t.creator_id,t.parent_id,t.status,t.description,t.budget,t.remaining
                FROM claim_tasks ct JOIN group_tasks t ON t.id=ct.task_id
                WHERE ct.group_id=? AND ct.claim_id IN ({marks}) ORDER BY ct.rowid''', params)
            collect('messages', f'''SELECT cm.claim_id,m.* FROM claim_messages cm
                JOIN group_messages m ON m.id=cm.message_id WHERE cm.group_id=?
                AND cm.claim_id IN ({marks}) ORDER BY cm.rowid''', params)
            collect('artifacts', f'''SELECT ca.claim_id,a.* FROM claim_artifacts ca
                JOIN group_artifacts a ON a.id=ca.artifact_id WHERE ca.group_id=?
                AND ca.claim_id IN ({marks}) ORDER BY ca.rowid''', params)
            collect('outcomes', f'''SELECT ca.claim_id,o.* FROM claim_artifacts ca
                JOIN group_artifact_outcomes o ON o.artifact_id=ca.artifact_id WHERE ca.group_id=?
                AND ca.claim_id IN ({marks}) ORDER BY o.rowid''', params)
            for claim in result['claims']:
                claim['imports'] = json.loads(claim['imports'])
            binding = db.execute('SELECT identity FROM artifact_verifier_binding WHERE id=1').fetchone()[0]
            for artifact in result['artifacts']:
                artifact['imports'] = json.loads(artifact['imports'])
                artifact['current_status'] = (
                    'verified' if verifier_identity is not None and
                    verifier_identity == binding == artifact['verifier_identity'] else 'needs_recheck'
                ) if artifact['status'] == 'verified' else artifact['status']
            # This is an inspection budget, not a worker packet builder. Include
            # JSON encoding overhead and remove whole rows, never truncate claims.
            categories = ('outcomes', 'artifacts', 'messages', 'tasks', 'reviews',
                          'publications', 'relationships', 'claims')
            for category in categories:
                while result[category] and len(json.dumps(result).encode()) > max_bytes:
                    result[category].pop()
                    result['omitted'][category] += 1
            if len(json.dumps(result).encode()) > max_bytes:
                raise ValueError('max_bytes too small for graph metadata')
            return result
