package ffmpeg

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestHomeKitTranscodeArgs(t *testing.T) {
	args := parseArgs(
		"rtsp://127.0.0.1:8554/ezviz#video=h264#audio=opus",
	).String()

	require.Contains(t, args, "-c:v libx264")
	require.Contains(t, args, "-c:a libopus")
	require.Contains(t, args, "-rtsp_transport tcp")
}
