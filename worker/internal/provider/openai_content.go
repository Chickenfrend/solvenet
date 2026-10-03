package provider

import (
	"fmt"
	"regexp"
	"strings"
	"unicode/utf16"

	"solvenet/worker/internal/daemon"
)

// Validate the original generated envelope before the shared v1 extractors
// decode into maps, which would silently discard earlier duplicate fields.
func extractOpenAIOutput(raw string, job daemon.Job) (string, error) {
	if _, err := decodeRawJSON(raw); err != nil {
		return "", fmt.Errorf("expected one valid JSON envelope without duplicate fields")
	}
	return extractOutput(raw, job)
}

// Unlike bounded diagnostics, result content must retain exact formal strings.
// Parse nested JSON only to redact a recoverable credential; leave clean JSON
// byte-for-byte intact, including Lean literal escapes embedded in its strings.
func redactOpenAIContent(content, key string) (string, error) {
	parts := make([]string, 0, len(key))
	for _, r := range key {
		units := utf16.Encode([]rune{r})
		escaped := ""
		for _, unit := range units {
			escaped += fmt.Sprintf(`\\u(?i:%04x)`, unit)
		}
		parts = append(parts, "(?:"+regexp.QuoteMeta(string(r))+"|"+escaped+")")
	}
	pattern := regexp.MustCompile(strings.Join(parts, ""))
	var sanitizeString func(string, int) (string, error)
	var sanitizeValue func(any, int) (any, bool, error)
	sanitizeValue = func(value any, depth int) (any, bool, error) {
		if depth > 64 {
			return nil, false, fmt.Errorf("nested output limit")
		}
		changed := false
		switch v := value.(type) {
		case map[string]any:
			result := make(map[string]any, len(v))
			for name, child := range v {
				n, err := sanitizeString(name, depth+1)
				if err != nil {
					return nil, false, err
				}
				c, dirty, err := sanitizeValue(child, depth+1)
				if err != nil {
					return nil, false, err
				}
				changed = changed || n != name || dirty
				if _, exists := result[n]; exists {
					return nil, false, fmt.Errorf("redacted key collision")
				}
				result[n] = c
			}
			return result, changed, nil
		case []any:
			for i, child := range v {
				c, dirty, err := sanitizeValue(child, depth+1)
				if err != nil {
					return nil, false, err
				}
				v[i], changed = c, changed || dirty
			}
			return v, changed, nil
		case string:
			s, err := sanitizeString(v, depth+1)
			return s, s != v, err
		default:
			return value, false, nil
		}
	}
	sanitizeString = func(s string, depth int) (string, error) {
		if depth > 64 {
			return "", fmt.Errorf("nested output limit")
		}
		if value, err := decodeRawJSON(s); err == nil {
			clean, changed, err := sanitizeValue(value, depth+1)
			if err != nil {
				return "", err
			}
			if changed {
				return encodeOpenAIRaw(clean)
			}
			return s, nil
		}
		trimmed := strings.TrimSpace(s)
		if strings.HasPrefix(trimmed, "{") || strings.HasPrefix(trimmed, "[") {
			return "", fmt.Errorf("malformed structured output")
		}
		return pattern.ReplaceAllString(s, "[redacted]"), nil
	}
	return sanitizeString(content, 0)
}
