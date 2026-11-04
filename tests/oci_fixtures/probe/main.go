// A static, network-free process for real OCI execution, not a shell image.
package main

import (
	"fmt"
	"os"
	"syscall"
)

func main() {
	if len(os.Args) != 2 {
		panic("one probe mode required")
	}
	switch os.Args[1] {
	case "baseline", "restricted":
		_, _, errno := syscall.RawSyscall(syscall.SYS_GETPPID, 0, 0, 0)
		file, err := os.Open("/dev/zero")
		if file != nil {
			_ = file.Close()
		}
		if os.Args[1] == "restricted" {
			if errno != syscall.EPERM {
				panic(fmt.Sprintf("seccomp getppid: %v", errno))
			}
			if !os.IsPermission(err) {
				panic(fmt.Sprintf("cgroup device /dev/zero: %v", err))
			}
		} else if errno != 0 || err != nil {
			panic(fmt.Sprintf("baseline: syscall=%v device=%v", errno, err))
		}
		// Keep an allowed device as the positive half of the cgroup policy test.
		null, err := os.OpenFile("/dev/null", os.O_WRONLY, 0)
		if err != nil {
			panic(err)
		}
		_ = null.Close()
		fmt.Println("OCI_PROBE_OK", os.Args[1])
	case "build":
		data, err := os.ReadFile("/data/file")
		if err != nil || string(data) != "archive-payload" {
			panic("ADD lost file payload")
		}
		for _, path := range []string{"/data/sym", "/data/hard"} {
			data, err := os.ReadFile(path)
			if err != nil || string(data) != "archive-payload" {
				panic("ADD lost link payload")
			}
		}
		if err := os.WriteFile("/result", []byte("BUILD_PROBE_OK\n"), 0644); err != nil {
			panic(err)
		}
	default:
		panic("unknown probe mode")
	}
}
