package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/minio/minio-go/v7"
)

// cmdLatest lists objects in the R2 bucket with the backup prefix, selects the
// newest by parsed timestamp, downloads it to destDir (or to the exact path if
// destDir is an existing file path), and prints the local path.
// Used by restore-drill.sh to fetch the most recent backup for verification.
func cmdLatest(cfg *Config, destDir string) error {
	client, err := newS3Client(cfg)
	if err != nil {
		return err
	}
	ctx := context.Background()

	type obj struct {
		key  string
		date time.Time
	}
	var all []obj
	for object := range client.ListObjects(ctx, cfg.S3.Bucket, minio.ListObjectsOptions{Prefix: "homelab-backup-"}) {
		if object.Err != nil {
			return object.Err
		}
		d, ok := parseBackupDate(object.Key)
		if !ok {
			continue
		}
		all = append(all, obj{key: object.Key, date: d})
	}
	if len(all) == 0 {
		return fmt.Errorf("no backups found in bucket %s", cfg.S3.Bucket)
	}
	sort.Slice(all, func(i, j int) bool { return all[i].date.After(all[j].date) })
	newest := all[0]
	slog.Info("latest", "key", newest.key, "date", newest.date.Format(time.RFC3339))

	// If destDir names an existing file, download there; otherwise join key.
	dest := destDir
	if info, err := os.Stat(destDir); err != nil || info.IsDir() {
		dest = filepath.Join(destDir, newest.key)
	}
	if err := os.MkdirAll(filepath.Dir(dest), 0755); err != nil {
		return err
	}

	if err := client.FGetObject(ctx, cfg.S3.Bucket, newest.key, dest, minio.GetObjectOptions{}); err != nil {
		return fmt.Errorf("download: %w", err)
	}
	info, _ := os.Stat(dest)
	slog.Info("downloaded", "dest", dest, "size", formatSize(info.Size()))
	fmt.Println(dest)
	return nil
}

// cmdList prints every backup object in the bucket (key + parsed date + size),
// newest-first. Used by the backup-health skill to report R2 contents without
// needing the aws CLI (which is not installed on this host) or rclone.
func cmdList(cfg *Config) error {
	client, err := newS3Client(cfg)
	if err != nil {
		return err
	}
	ctx := context.Background()

	type entry struct {
		key  string
		date time.Time
		size int64
	}
	var all []entry
	for object := range client.ListObjects(ctx, cfg.S3.Bucket, minio.ListObjectsOptions{Prefix: "homelab-backup-"}) {
		if object.Err != nil {
			return object.Err
		}
		d, ok := parseBackupDate(object.Key)
		if !ok {
			continue
		}
		all = append(all, entry{key: object.Key, date: d, size: object.Size})
	}
	sort.Slice(all, func(i, j int) bool { return all[i].date.After(all[j].date) })
	fmt.Printf("%-44s %-20s %s\n", "KEY", "DATE (UTC)", "SIZE")
	if len(all) == 0 {
		fmt.Println("(no backups in bucket)")
		return nil
	}
	for _, e := range all {
		fmt.Printf("%-44s %s %s\n", e.key, e.date.Format("2006-01-02 15:04:05"), formatSize(e.size))
	}
	slog.Info("listed", "count", len(all))
	return nil
}

// formatSize lives in backup.go but is referenced here; the compiler links it.
var _ = strings.TrimSpace