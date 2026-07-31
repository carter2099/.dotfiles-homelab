package main

import (
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

type Config struct {
	Schedule  string          `yaml:"schedule"`
	BackupDir string          `yaml:"backup_dir"`
	Retention RetentionConfig `yaml:"retention"`
	S3        S3Config        `yaml:"s3"`
	Targets   []Target        `yaml:"targets"`
}

type RetentionConfig struct {
	DailyDays    int `yaml:"daily_days"`
	MonthlyCount int `yaml:"monthly_count"`
	YearlyCount  int `yaml:"yearly_count"`
}

type S3Config struct {
	Endpoint        string `yaml:"endpoint"`
	Bucket          string `yaml:"bucket"`
	Region          string `yaml:"region"`
	AccessKeyID     string `yaml:"access_key_id"`
	SecretAccessKey string `yaml:"secret_access_key"`
}

type Target struct {
	Name      string   `yaml:"name"`
	Type      string   `yaml:"type"`
	Source    string   `yaml:"source"`
	Container string   `yaml:"container"`
	DBPath    string   `yaml:"db_path"`
	Sudo      bool     `yaml:"sudo"`
	Excludes  []string `yaml:"excludes"`
}

func LoadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}

	cfg := &Config{
		BackupDir: "/home/carter/backups",
		Schedule:  "0 3 * * *",
		Retention: RetentionConfig{
			DailyDays:    14,
			MonthlyCount: 1,
			YearlyCount:  1,
		},
	}

	if err := yaml.Unmarshal(data, cfg); err != nil {
		return nil, err
	}

	loadDotEnvIfAbsent()

	if env := os.Getenv("R2_ACCESS_KEY_ID"); env != "" {
		cfg.S3.AccessKeyID = env
	}
	if env := os.Getenv("R2_SECRET_ACCESS_KEY"); env != "" {
		cfg.S3.SecretAccessKey = env
	}

	return cfg, nil
}

// loadDotEnvIfAbsent sources R2 credentials from the .env file next to the
// executable when the environment doesn't already provide them. The systemd
// unit passes them via EnvironmentFile; manual/agent invocations (list,
// latest, restore drill) otherwise fail with an Authorization error.
func loadDotEnvIfAbsent() {
	if os.Getenv("R2_ACCESS_KEY_ID") != "" && os.Getenv("R2_SECRET_ACCESS_KEY") != "" {
		return
	}
	exe, err := os.Executable()
	if err != nil {
		return
	}
	data, err := os.ReadFile(filepath.Join(filepath.Dir(exe), ".env"))
	if err != nil {
		return
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		if key = strings.TrimSpace(key); key == "" {
			continue
		}
		if _, exists := os.LookupEnv(key); !exists {
			os.Setenv(key, strings.TrimSpace(value))
		}
	}
}
