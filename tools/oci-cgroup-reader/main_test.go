package main

import (
	"bytes"
	"errors"
	"io"
	"strings"
	"testing"
)

type brokenReader struct{}

func (brokenReader) Read(p []byte) (int, error) { return 0, errors.New("fixture read failure") }

type shortWriter struct{}

func (shortWriter) Write(p []byte) (int, error) { return len(p) - 1, nil }

type brokenWriter struct{}

func (brokenWriter) Write(p []byte) (int, error) { return 0, errors.New("fixture write failure") }

func TestExactMembership(t *testing.T) {
	want := "0::/init\n"
	var output bytes.Buffer
	if err := emitCgroup(strings.NewReader(want), &output); err != nil || output.String() != want {
		t.Fatalf("membership not preserved: %q, %v", output.String(), err)
	}
}

func TestInvalidInputProducesNoOutput(t *testing.T) {
	fixtures := map[string]io.Reader{
		"empty":        strings.NewReader(""),
		"truncated":    strings.NewReader("0::/init"),
		"nul":          strings.NewReader("0::/\x00\n"),
		"oversized":    strings.NewReader(strings.Repeat("x", maxCgroupBytes) + "\n"),
		"read failure": brokenReader{},
	}
	for name, fixture := range fixtures {
		t.Run(name, func(t *testing.T) {
			var output bytes.Buffer
			if err := emitCgroup(fixture, &output); err == nil || output.Len() != 0 {
				t.Fatalf("invalid membership accepted or partial output emitted: %v", err)
			}
		})
	}
}

func TestShortWriteFails(t *testing.T) {
	if err := emitCgroup(strings.NewReader("0::/\n"), shortWriter{}); err == nil {
		t.Fatal("short write accepted")
	}
}

func TestExactSizeBoundary(t *testing.T) {
	want := "0::/" + strings.Repeat("a", maxCgroupBytes-5) + "\n"
	var output bytes.Buffer
	if err := emitCgroup(strings.NewReader(want), &output); err != nil || output.String() != want {
		t.Fatalf("valid exact-size membership was not preserved: %v", err)
	}
}

func TestWriterErrorFails(t *testing.T) {
	if err := emitCgroup(strings.NewReader("0::/\n"), brokenWriter{}); err == nil {
		t.Fatal("writer error accepted")
	}
}
