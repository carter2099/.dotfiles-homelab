package main

import (
	"archive/tar"
	"compress/gzip"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
)

// cmdVerify downloads nothing — it inspects a local archive:
//   - confirms it is a readable tar.gz
//   - lists the top-level target directories present
//   - extracts every *.db / *.sqlite3 / *.sqlite and runs PRAGMA integrity_check
//
// Exit 0 if the archive is readable and every embedded DB passes; 1 otherwise.
// Used by restore-drill.sh to prove backups are restorable without nuking prod.
func cmdVerify(archivePath string) error {
	f, err := os.Open(archivePath)
	if err != nil {
		return fmt.Errorf("open archive: %w", err)
	}
	defer f.Close()

	gz, err := gzip.NewReader(f)
	if err != nil {
		return fmt.Errorf("gunzip: %w (corrupt or not a gzip archive?)", err)
	}
	defer gz.Close()
	tr := tar.NewReader(gz)

	tmpDir, err := os.MkdirTemp("", "hb-verify-*")
	if err != nil {
		return err
	}
	defer os.RemoveAll(tmpDir)

	topSet := make(map[string]bool)
	var dbFiles []string

	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return fmt.Errorf("read tar entry: %w", err)
		}

		name := strings.TrimPrefix(filepath.Clean(hdr.Name), "/")
		if name == "" || name == "." {
			continue
		}
		// track top-level component = a target name
		top := strings.SplitN(name, "/", 2)[0]
		topSet[top] = true

		// only extract regular files that look like SQLite DBs
		if hdr.Typeflag != tar.TypeReg {
			continue
		}
		base := strings.ToLower(filepath.Base(name))
		if isSQLiteFile(base) {
			dest := filepath.Join(tmpDir, name)
			if err := os.MkdirAll(filepath.Dir(dest), 0755); err != nil {
				return err
			}
			out, err := os.Create(dest)
			if err != nil {
				return err
			}
			if _, err := io.Copy(out, tr); err != nil {
				out.Close()
				return err
			}
			out.Close()
			dbFiles = append(dbFiles, dest)
		}
	}

	// Report target manifest
	topics := make([]string, 0, len(topSet))
	for k := range topSet {
		topics = append(topics, k)
	}
	sort.Strings(topics)
	slog.Info("verify manifest", "targets", len(topics))
	for _, t := range topics {
		fmt.Println("  target:", t)
	}

	if len(dbFiles) == 0 {
		slog.Warn("verify: no SQLite databases found in archive")
		fmt.Println("VERDICT: PASS (archive readable, no DBs to integrity-check)")
		return nil
	}

	bad := 0
	for _, db := range dbFiles {
		rel, _ := filepath.Rel(tmpDir, db)
		ok, msg := sqliteIntegrityOK(db)
		if ok {
			fmt.Printf("  OK    %-60s integrity_check=ok\n", rel)
		} else {
			fmt.Printf("  FAIL  %-60s %s\n", rel, msg)
			bad++
		}
	}

	if bad > 0 {
		fmt.Printf("VERDICT: FAIL (%d/%d databases failed integrity check)\n", bad, len(dbFiles))
		return fmt.Errorf("%d databases failed integrity check", bad)
	}
	fmt.Printf("VERDICT: PASS (%d databases ok, %d targets present)\n", len(dbFiles), len(topics))
	return nil
}

func isSQLiteFile(base string) bool {
	return strings.HasSuffix(base, ".db") ||
		strings.HasSuffix(base, ".sqlite") ||
		strings.HasSuffix(base, ".sqlite3")
}

// sqliteIntegrityOK runs `sqlite3 <path> "PRAGMA integrity_check;"` and returns
// true only if the result is exactly "ok".
func sqliteIntegrityOK(path string) (bool, string) {
	cmd := exec.Command("sqlite3", path, "PRAGMA integrity_check;")
	out, err := cmd.CombinedOutput()
	if err != nil {
		return false, fmt.Sprintf("sqlite3 error: %s: %v", strings.TrimSpace(string(out)), err)
	}
	res := strings.TrimSpace(string(out))
	if res != "ok" {
		return false, "integrity_check=" + res
	}
	return true, ""
}