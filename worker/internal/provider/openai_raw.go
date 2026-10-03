package provider

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"strings"

	"solvenet/worker/internal/daemon"
)

const unavailableOpenAIRaw = "[raw output unavailable: malformed or opaque JSON]"

// Retained diagnostics are separate from extraction: parse the complete original
// envelope, redact decoded strings (including nested JSON), and re-encode it.
// Never retain a partial envelope or assume malformed escaped text is safe.
func safeOpenAIRaw(raw, key string) *daemon.Generation {
	if len(raw) > daemon.MaxRawResponseBytes {
		return &daemon.Generation{RawResponse: "[raw output unavailable: size limit exceeded]", RawResponseTruncated: true}
	}
	value, err := decodeRawJSON(raw)
	if err != nil {
		return rawGeneration(unavailableOpenAIRaw)
	}
	value = redactRawJSON(value, key, 0)
	encoded, err := encodeOpenAIRaw(value)
	if err != nil {
		return rawGeneration(unavailableOpenAIRaw)
	}
	if len(encoded) > daemon.MaxRawResponseBytes {
		// Diagnostic sanitization must not turn a bounded original into a
		// truncated model generation. Omit only the expanded diagnostic.
		return rawGeneration("[raw output unavailable: size limit exceeded]")
	}
	return rawGeneration(encoded)
}

func encodeOpenAIRaw(value any) (string, error) {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return "", err
	}
	return strings.TrimSuffix(buffer.String(), "\n"), nil
}

func decodeRawJSON(raw string) (any, error) {
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.UseNumber()
	value, err := decodeOpenAIValue(decoder, 0)
	if err != nil {
		return nil, err
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		return nil, fmt.Errorf("expected one JSON value")
	}
	return value, nil
}

// Duplicate fields cannot be inspected safely by decoding into a map: earlier
// credential-bearing values would disappear while the original bytes survived.
func decodeOpenAIValue(decoder *json.Decoder, depth int) (any, error) {
	if depth > 128 {
		return nil, fmt.Errorf("JSON nesting limit")
	}
	token, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	delimiter, compound := token.(json.Delim)
	if !compound {
		return token, nil
	}
	switch delimiter {
	case '{':
		value := map[string]any{}
		for decoder.More() {
			name, err := decoder.Token()
			if err != nil {
				return nil, err
			}
			key, ok := name.(string)
			if !ok {
				return nil, fmt.Errorf("invalid JSON key")
			}
			if _, exists := value[key]; exists {
				return nil, fmt.Errorf("duplicate JSON key")
			}
			child, err := decodeOpenAIValue(decoder, depth+1)
			if err != nil {
				return nil, err
			}
			value[key] = child
		}
		_, err := decoder.Token()
		return value, err
	case '[':
		value := []any{}
		for decoder.More() {
			child, err := decodeOpenAIValue(decoder, depth+1)
			if err != nil {
				return nil, err
			}
			value = append(value, child)
		}
		_, err := decoder.Token()
		return value, err
	default:
		return nil, fmt.Errorf("unexpected JSON delimiter")
	}
}

func redactRawJSON(value any, key string, depth int) any {
	if depth >= 16 {
		return unavailableOpenAIRaw
	}
	switch v := value.(type) {
	case map[string]any:
		result := make(map[string]any, len(v))
		for name, child := range v {
			result[redactRawString(name, key, depth+1)] = redactRawJSON(child, key, depth+1)
		}
		return result
	case []any:
		for i, child := range v {
			v[i] = redactRawJSON(child, key, depth+1)
		}
		return v
	case string:
		return redactRawString(v, key, depth+1)
	default:
		// Numbers, booleans and null cannot contain credentials.
		return value
	}
}

func redactRawString(value, key string, depth int) string {
	if depth >= 16 {
		return unavailableOpenAIRaw
	}
	value = strings.ReplaceAll(value, key, "[redacted]")
	if value == "[redacted]" {
		return value
	}
	trimmed := strings.TrimSpace(value)
	if nested, err := decodeRawJSON(trimmed); err == nil {
		encoded, err := encodeOpenAIRaw(redactRawJSON(nested, key, depth+1))
		if err != nil {
			return unavailableOpenAIRaw
		}
		return encoded
	}
	// Strings purporting to contain JSON or containing residual escape syntax
	// cannot be structurally checked. Keep the envelope shape, not their bytes.
	if strings.HasPrefix(trimmed, "{") || strings.HasPrefix(trimmed, "[") || strings.HasPrefix(trimmed, `"`) || strings.Contains(value, `\`) {
		return unavailableOpenAIRaw
	}
	return value
}
