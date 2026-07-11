package main

import (
	"archive/tar"
	"compress/gzip"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

func RunBackup(cfg *Config, localOnly bool) error {
	timestamp := time.Now().UTC().Format("20060102-150405")
	archiveName := fmt.Sprintf("homelab-backup-%s.tar.gz", timestamp)

	stagingDir, err := os.MkdirTemp("", "homelab-backup-*")
	if err != nil {
		return fmt.Errorf("create staging dir: %w", err)
	}
	defer os.RemoveAll(stagingDir)

	var failures []string
	for _, target := range cfg.Targets {
		slog.Info("backing up", "target", target.Name, "type", target.Type)
		if err := backupTarget(stagingDir, target); err != nil {
			slog.Error("target failed", "target", target.Name, "error", err)
			failures = append(failures, fmt.Sprintf("%s: %v", target.Name, err))
			continue
		}
		slog.Info("done", "target", target.Name)
	}

	if err := os.MkdirAll(cfg.BackupDir, 0755); err != nil {
		return fmt.Errorf("create backup dir: %w", err)
	}

	archivePath := filepath.Join(cfg.BackupDir, archiveName)
	if err := createArchive(archivePath, stagingDir); err != nil {
		return fmt.Errorf("create archive: %w", err)
	}

	info, _ := os.Stat(archivePath)
	slog.Info("archive created", "path", archivePath, "size", formatSize(info.Size()))

	if !localOnly && cfg.S3.Bucket != "" && cfg.S3.AccessKeyID != "" {
		slog.Info("uploading", "bucket", cfg.S3.Bucket)
		if err := Upload(cfg, archivePath, archiveName); err != nil {
			return fmt.Errorf("upload: %w", err)
		}
		if err := ApplyRetention(cfg); err != nil {
			slog.Warn("retention cleanup failed", "error", err)
		}
	} else {
		slog.Info("skipping upload (no S3 credentials or --local-only)")
	}

	cleanLocalBackups(cfg.BackupDir, cfg.Retention.DailyDays)

	if len(failures) > 0 {
		return fmt.Errorf("partial backup, %d target(s) failed: %s", len(failures), strings.Join(failures, "; "))
	}

	return nil
}

func backupTarget(stagingDir string, t Target) error {
	targetDir := filepath.Join(stagingDir, t.Name)
	if err := os.MkdirAll(targetDir, 0755); err != nil {
		return err
	}

	switch t.Type {
	case "directory":
		return backupDirectory(targetDir, t)
	case "sqlite-docker":
		return backupSQLiteDocker(targetDir, t)
	case "sqlite-host":
		return backupSQLiteHost(targetDir, t)
	default:
		return fmt.Errorf("unknown target type: %s", t.Type)
	}
}

func backupDirectory(destDir string, t Target) error {
	if t.Sudo {
		return backupDirectorySudo(destDir, t)
	}

	return filepath.WalkDir(t.Source, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}

		relPath, _ := filepath.Rel(t.Source, path)

		if d.IsDir() && shouldExclude(relPath, t.Excludes) {
			return filepath.SkipDir
		}
		if !d.IsDir() && shouldExclude(relPath, t.Excludes) {
			return nil
		}

		destPath := filepath.Join(destDir, relPath)
		if d.IsDir() {
			return os.MkdirAll(destPath, 0755)
		}

		return copyFile(path, destPath)
	})
}

func backupDirectorySudo(destDir string, t Target) error {
	args := []string{"rsync", "-a", "--chown=carter:carter"}
	for _, exc := range t.Excludes {
		args = append(args, "--exclude="+exc)
	}
	args = append(args, t.Source+"/", destDir+"/")

	cmd := exec.Command("sudo", args...)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("rsync: %s: %w", strings.TrimSpace(string(out)), err)
	}
	return nil
}

func backupSQLiteDocker(destDir string, t Target) error {
	tmpName := "hlbackup-" + filepath.Base(t.DBPath)
	containerTmp := "/tmp/" + tmpName

	cmd := exec.Command("docker", "exec", t.Container,
		"sqlite3", t.DBPath, ".backup "+containerTmp)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("sqlite3 .backup: %s: %w", strings.TrimSpace(string(out)), err)
	}

	destFile := filepath.Join(destDir, filepath.Base(t.DBPath))
	cmd = exec.Command("docker", "cp", t.Container+":"+containerTmp, destFile)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("docker cp: %s: %w", strings.TrimSpace(string(out)), err)
	}

	exec.Command("docker", "exec", t.Container, "rm", containerTmp).Run()

	return verifySQLite(destFile)
}

func backupSQLiteHost(destDir string, t Target) error {
	destFile := filepath.Join(destDir, filepath.Base(t.Source))

	var cmd *exec.Cmd
	if t.Sudo {
		cmd = exec.Command("sudo", "sqlite3", t.Source, ".backup "+destFile)
	} else {
		cmd = exec.Command("sqlite3", t.Source, ".backup "+destFile)
	}

	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("sqlite3 .backup: %s: %w", strings.TrimSpace(string(out)), err)
	}

	return verifySQLite(destFile)
}

func verifySQLite(path string) error {
	cmd := exec.Command("sqlite3", path, "PRAGMA integrity_check;")
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("integrity check failed: %s: %w", strings.TrimSpace(string(out)), err)
	}
	result := strings.TrimSpace(string(out))
	if result != "ok" {
		return fmt.Errorf("integrity check: %s", result)
	}
	slog.Info("integrity ok", "file", filepath.Base(path))
	return nil
}

func shouldExclude(relPath string, excludes []string) bool {
	for _, pattern := range excludes {
		base := filepath.Base(relPath)
		if matched, _ := filepath.Match(pattern, base); matched {
			return true
		}
		if matched, _ := filepath.Match(pattern, relPath); matched {
			return true
		}
	}
	return false
}

func createArchive(archivePath, sourceDir string) error {
	f, err := os.Create(archivePath)
	if err != nil {
		return err
	}
	defer f.Close()

	gw := gzip.NewWriter(f)
	defer gw.Close()

	tw := tar.NewWriter(gw)
	defer tw.Close()

	return filepath.WalkDir(sourceDir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}

		relPath, _ := filepath.Rel(sourceDir, path)
		if relPath == "." {
			return nil
		}

		info, err := d.Info()
		if err != nil {
			return err
		}

		header, err := tar.FileInfoHeader(info, "")
		if err != nil {
			return err
		}
		header.Name = relPath

		if d.IsDir() {
			header.Name += "/"
		}

		if err := tw.WriteHeader(header); err != nil {
			return err
		}

		if d.IsDir() {
			return nil
		}

		file, err := os.Open(path)
		if err != nil {
			return err
		}
		defer file.Close()

		_, err = io.Copy(tw, file)
		return err
	})
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()

	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()

	_, err = io.Copy(out, in)
	return err
}

func cleanLocalBackups(dir string, keepDays int) {
	cutoff := time.Now().AddDate(0, 0, -keepDays)
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, e := range entries {
		if !strings.HasPrefix(e.Name(), "homelab-backup-") || !strings.HasSuffix(e.Name(), ".tar.gz") {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		if info.ModTime().Before(cutoff) {
			path := filepath.Join(dir, e.Name())
			if err := os.Remove(path); err == nil {
				slog.Info("deleted local backup", "file", e.Name())
			}
		}
	}
}

func formatSize(bytes int64) string {
	const (
		KB = 1024
		MB = KB * 1024
		GB = MB * 1024
	)
	switch {
	case bytes >= GB:
		return fmt.Sprintf("%.1f GB", float64(bytes)/float64(GB))
	case bytes >= MB:
		return fmt.Sprintf("%.1f MB", float64(bytes)/float64(MB))
	case bytes >= KB:
		return fmt.Sprintf("%.1f KB", float64(bytes)/float64(KB))
	default:
		return fmt.Sprintf("%d B", bytes)
	}
}
