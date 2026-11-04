package archivequalification

import (
	"archive/tar"
	"bytes"
	"os"
	"path/filepath"
	"testing"

	archive "github.com/moby/go-archive"
)

type entry struct {
	name, link, data string
	kind             byte
}

func unpack(t *testing.T, dest string, entries ...entry) error {
	t.Helper()
	var data bytes.Buffer
	w := tar.NewWriter(&data)
	for _, item := range entries {
		h := &tar.Header{Name: item.name, Linkname: item.link, Typeflag: item.kind,
			Mode: 0644, Uid: os.Getuid(), Gid: os.Getgid(), Size: int64(len(item.data))}
		if err := w.WriteHeader(h); err != nil {
			t.Fatal(err)
		}
		if _, err := w.Write([]byte(item.data)); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	return archive.Untar(&data, dest, &archive.TarOptions{BestEffortXattrs: true})
}

func TestTraversalAndLinksCannotWriteOutsideDestination(t *testing.T) {
	for _, kind := range []string{"parent", "symlink", "hardlink"} {
		t.Run(kind, func(t *testing.T) {
			root := t.TempDir()
			dest := filepath.Join(root, "dest")
			if err := os.Mkdir(dest, 0755); err != nil {
				t.Fatal(err)
			}
			sentinel := filepath.Join(root, "sentinel")
			if err := os.WriteFile(sentinel, []byte("unchanged"), 0600); err != nil {
				t.Fatal(err)
			}
			var items []entry
			switch kind {
			case "parent":
				items = []entry{{name: "../sentinel", data: "escaped", kind: tar.TypeReg}}
			case "symlink":
				items = []entry{{name: "escape", link: "..", kind: tar.TypeSymlink}, {name: "escape/sentinel", data: "escaped", kind: tar.TypeReg}}
			case "hardlink":
				items = []entry{{name: "escape", link: "../sentinel", kind: tar.TypeLink}, {name: "escape", data: "escaped", kind: tar.TypeReg}}
			}
			// Rejection and safe confinement are both valid; a host-side write is not.
			_ = unpack(t, dest, items...)
			got, err := os.ReadFile(sentinel)
			if err != nil || string(got) != "unchanged" {
				t.Fatalf("outside write: %q %v", got, err)
			}
		})
	}
}

func TestOrdinaryFileSymlinkHardlinkAndOwnership(t *testing.T) {
	dest := t.TempDir()
	err := unpack(t, dest,
		entry{name: "file", data: "payload", kind: tar.TypeReg},
		entry{name: "sym", link: "file", kind: tar.TypeSymlink},
		entry{name: "hard", link: "file", kind: tar.TypeLink})
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"file", "sym", "hard"} {
		got, err := os.ReadFile(filepath.Join(dest, name))
		if err != nil || string(got) != "payload" {
			t.Fatalf("%s: %q %v", name, got, err)
		}
	}
	a, err := os.Stat(filepath.Join(dest, "file"))
	if err != nil {
		t.Fatal(err)
	}
	b, err := os.Stat(filepath.Join(dest, "hard"))
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(a, b) {
		t.Fatal("hardlink became a copy")
	}
}
