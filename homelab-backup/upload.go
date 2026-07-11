package main

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

func newS3Client(cfg *Config) (*minio.Client, error) {
	endpoint := cfg.S3.Endpoint
	useSSL := true
	if strings.HasPrefix(endpoint, "https://") {
		endpoint = strings.TrimPrefix(endpoint, "https://")
	} else if strings.HasPrefix(endpoint, "http://") {
		endpoint = strings.TrimPrefix(endpoint, "http://")
		useSSL = false
	}

	return minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(cfg.S3.AccessKeyID, cfg.S3.SecretAccessKey, ""),
		Secure: useSSL,
		Region: cfg.S3.Region,
	})
}

func Upload(cfg *Config, filePath, objectName string) error {
	client, err := newS3Client(cfg)
	if err != nil {
		return fmt.Errorf("create S3 client: %w", err)
	}

	ctx := context.Background()
	_, err = client.FPutObject(ctx, cfg.S3.Bucket, objectName, filePath, minio.PutObjectOptions{
		ContentType: "application/gzip",
	})
	if err != nil {
		return fmt.Errorf("upload: %w", err)
	}

	slog.Info("uploaded", "bucket", cfg.S3.Bucket, "object", objectName)
	return nil
}

type backupEntry struct {
	key  string
	date time.Time
}

func parseBackupDate(key string) (time.Time, bool) {
	// homelab-backup-20260412-225953.tar.gz
	prefix := "homelab-backup-"
	if !strings.HasPrefix(key, prefix) {
		return time.Time{}, false
	}
	name := strings.TrimPrefix(key, prefix)
	name = strings.TrimSuffix(name, ".tar.gz")
	t, err := time.Parse("20060102-150405", name)
	if err != nil {
		return time.Time{}, false
	}
	return t, true
}

// ApplyRetention keeps: all dailies within DailyDays, the newest MonthlyCount
// backups between DailyDays and 365 days old, and the newest YearlyCount
// backups older than 365 days. Deletes everything else.
func ApplyRetention(cfg *Config) error {
	client, err := newS3Client(cfg)
	if err != nil {
		return err
	}

	ctx := context.Background()
	now := time.Now()
	dailyCutoff := now.AddDate(0, 0, -cfg.Retention.DailyDays)
	yearlyCutoff := now.AddDate(-1, 0, 0)

	var all []backupEntry
	for object := range client.ListObjects(ctx, cfg.S3.Bucket, minio.ListObjectsOptions{Prefix: "homelab-backup-"}) {
		if object.Err != nil {
			return object.Err
		}
		date, ok := parseBackupDate(object.Key)
		if !ok {
			continue
		}
		all = append(all, backupEntry{key: object.Key, date: date})
	}

	sort.Slice(all, func(i, j int) bool {
		return all[i].date.After(all[j].date)
	})

	keep := make(map[string]string) // key → reason
	var monthly, yearly []backupEntry

	for _, b := range all {
		switch {
		case b.date.After(dailyCutoff):
			keep[b.key] = "daily"
		case b.date.After(yearlyCutoff):
			monthly = append(monthly, b)
		default:
			yearly = append(yearly, b)
		}
	}

	// monthly and yearly are already sorted newest-first
	for i, b := range monthly {
		if i < cfg.Retention.MonthlyCount {
			keep[b.key] = "monthly"
		}
	}
	for i, b := range yearly {
		if i < cfg.Retention.YearlyCount {
			keep[b.key] = "yearly"
		}
	}

	var deleted int
	for _, b := range all {
		if _, ok := keep[b.key]; ok {
			continue
		}
		if err := client.RemoveObject(ctx, cfg.S3.Bucket, b.key, minio.RemoveObjectOptions{}); err != nil {
			slog.Warn("failed to delete", "key", b.key, "error", err)
			continue
		}
		slog.Info("deleted", "key", b.key, "age", now.Sub(b.date).Round(time.Hour))
		deleted++
	}

	if deleted > 0 || len(all) > 0 {
		slog.Info("retention applied", "kept", len(keep), "deleted", deleted,
			"breakdown", fmt.Sprintf("daily=%d monthly=%d yearly=%d",
				countReason(keep, "daily"), countReason(keep, "monthly"), countReason(keep, "yearly")))
	}

	return nil
}

func countReason(m map[string]string, reason string) int {
	n := 0
	for _, v := range m {
		if v == reason {
			n++
		}
	}
	return n
}
