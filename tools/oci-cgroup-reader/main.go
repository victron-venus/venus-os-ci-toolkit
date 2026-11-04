// cgroup-reader prints only the container init process's cgroup membership.
package main

import (
	"bytes"
	"fmt"
	"io"
	"os"
)

const maxCgroupBytes = 64 * 1024

func emitCgroup(reader io.Reader, writer io.Writer) error {
	data, err := io.ReadAll(io.LimitReader(reader, maxCgroupBytes+1))
	if err != nil {
		return fmt.Errorf("cannot read cgroup membership")
	}
	if len(data) == 0 || len(data) > maxCgroupBytes || data[len(data)-1] != '\n' || bytes.IndexByte(data, 0) >= 0 {
		return fmt.Errorf("invalid or oversized cgroup membership")
	}
	n, err := writer.Write(data)
	if err != nil || n != len(data) {
		return fmt.Errorf("cannot write cgroup membership")
	}
	return nil
}

func main() {
	if len(os.Args) != 1 {
		fmt.Fprintln(os.Stderr, "arguments are not accepted")
		os.Exit(2)
	}
	file, err := os.Open("/proc/1/cgroup")
	if err != nil {
		fmt.Fprintln(os.Stderr, "cannot open cgroup membership")
		os.Exit(1)
	}
	err = emitCgroup(file, os.Stdout)
	closeErr := file.Close()
	if err != nil || closeErr != nil {
		fmt.Fprintln(os.Stderr, "cgroup membership read failed")
		os.Exit(1)
	}
}
