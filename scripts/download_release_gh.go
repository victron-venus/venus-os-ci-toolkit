// Download a reviewed release artifact with the qualified Go FIPS TLS profile.
package main

import (
	"crypto/fips140"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"time"
)

const maxArchive = 32 * 1024 * 1024

func download(address, output string) error {
	if runtime.Version() != "go1.27.3" || !fips140.Enabled() {
		return errors.New("downloader requires Go 1.27.3 and GODEBUG=fips140=on")
	}
	parsed, err := url.Parse(address)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil {
		return errors.New("download requires an HTTPS URL without credentials")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12}
	client := &http.Client{
		Transport: transport,
		Timeout:   60 * time.Second,
		CheckRedirect: func(request *http.Request, via []*http.Request) error {
			if request.URL.Scheme != "https" || request.URL.User != nil || len(via) >= 10 {
				return errors.New("redirect must retain HTTPS and stay within ten hops")
			}
			return nil
		},
	}
	response, err := client.Get(address)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("download returned HTTP %d", response.StatusCode)
	}
	if response.ContentLength > maxArchive {
		return errors.New("archive exceeds size limit")
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, maxArchive+1))
	if err != nil {
		return err
	}
	if len(data) > maxArchive {
		return errors.New("archive exceeds size limit")
	}
	file, err := os.OpenFile(output, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	_, writeErr := file.Write(data)
	closeErr := file.Close()
	if writeErr != nil {
		return writeErr
	}
	return closeErr
}

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: download_release_gh HTTPS_URL NEW_OUTPUT_FILE")
		os.Exit(2)
	}
	if err := download(os.Args[1], os.Args[2]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
