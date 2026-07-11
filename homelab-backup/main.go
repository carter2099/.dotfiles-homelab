package main

import (
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/robfig/cron/v3"
)

func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo})))

	if len(os.Args) < 2 {
		fmt.Fprintf(os.Stderr, "Usage: homelab-backup <run|daemon|verify <archive>|latest <dest>> [--config path] [--local-only]\n")
		os.Exit(1)
	}

	cmd := os.Args[1]
	configPath := ""
	localOnly := false

	for i := 2; i < len(os.Args); i++ {
		switch os.Args[i] {
		case "--config":
			if i+1 < len(os.Args) {
				configPath = os.Args[i+1]
				i++
			}
		case "--local-only":
			localOnly = true
		}
	}

	if configPath == "" {
		exe, _ := os.Executable()
		candidates := []string{
			filepath.Join(filepath.Dir(exe), "config.yaml"),
			"config.yaml",
		}
		for _, c := range candidates {
			if _, err := os.Stat(c); err == nil {
				configPath = c
				break
			}
		}
		if configPath == "" {
			slog.Error("no config.yaml found")
			os.Exit(1)
		}
	}

	cfg, err := LoadConfig(configPath)
	if err != nil {
		slog.Error("failed to load config", "error", err)
		os.Exit(1)
	}

	switch cmd {
	case "run":
		if err := RunBackup(cfg, localOnly); err != nil {
			slog.Error("backup failed", "error", err)
			os.Exit(1)
		}
		slog.Info("backup completed successfully")
	case "daemon":
		runDaemon(cfg, localOnly)
	case "verify":
		if len(os.Args) < 3 {
			fmt.Fprintf(os.Stderr, "Usage: homelab-backup verify <archive.tar.gz>\n")
			os.Exit(2)
		}
		if err := cmdVerify(os.Args[2]); err != nil {
			slog.Error("verify failed", "error", err)
			os.Exit(1)
		}
	case "latest":
		if len(os.Args) < 3 {
			fmt.Fprintf(os.Stderr, "Usage: homelab-backup latest <dest-dir>\n")
			os.Exit(2)
		}
		if err := cmdLatest(cfg, os.Args[2]); err != nil {
			slog.Error("latest failed", "error", err)
			os.Exit(1)
		}
	default:
		fmt.Fprintf(os.Stderr, "Unknown command: %s\n", cmd)
		os.Exit(1)
	}
}

func runDaemon(cfg *Config, localOnly bool) {
	slog.Info("starting daemon", "schedule", cfg.Schedule)

	c := cron.New()
	_, err := c.AddFunc(cfg.Schedule, func() {
		slog.Info("starting scheduled backup")
		if err := RunBackup(cfg, localOnly); err != nil {
			slog.Error("scheduled backup failed", "error", err)
		} else {
			slog.Info("scheduled backup completed")
		}
	})
	if err != nil {
		slog.Error("invalid cron schedule", "error", err)
		os.Exit(1)
	}

	c.Start()
	defer c.Stop()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	sig := <-sigCh
	slog.Info("shutting down", "signal", sig)
}
