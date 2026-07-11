package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
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