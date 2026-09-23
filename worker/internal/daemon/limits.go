package daemon

// v1 wire limits; keep in sync with coordinator/src/solvenet/protocol_limits.py
// and protocol/v1.md. Sizes are UTF-8 bytes, not characters.
const (
	MaxIdentifierBytes          = 256
	MaxStatementBytes           = 64 * 1024
	MaxImports                  = 32
	MaxClaimModels              = 32
	MaxImportBytes              = 256
	MaxMessages                 = 32
	MaxMessageContentBytes      = 256 * 1024
	MaxModelBytes               = 256
	MaxCandidateBytes           = 128 * 1024
	MaxRawResponseBytes         = 128 * 1024
	MaxFinishReasonBytes        = 256
	MaxErrorBytes               = 4096
	MaxOutputTokens             = 32768
	MaxGenerationTimeoutSeconds = 24 * 60 * 60
	MaxHeartbeatSeconds         = 24 * 60 * 60
	MaxRequestBytes             = 256 * 1024
	MaxResultRequestBytes       = 2 * 1024 * 1024
)
