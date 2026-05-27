package openers

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

type Store struct {
	Path   string
	Values map[string]string
}

func Defaults() map[string]string {
	return map[string]string{
		".orf":  "",
		".nef":  "",
		".jpg":  "",
		".jpeg": "",
		".png":  "",
		".tif":  "",
		".tiff": "",
		".psd":  "",
		"*":     "",
	}
}

func Load(configPath string) *Store {
	values := Defaults()
	data, err := os.ReadFile(configPath)
	if err == nil {
		var loaded map[string]string
		if json.Unmarshal(data, &loaded) == nil {
			for key, value := range loaded {
				values[strings.ToLower(key)] = strings.TrimSpace(value)
			}
		}
	}
	return &Store{Path: configPath, Values: values}
}

func (s *Store) Save() error {
	data, err := json.MarshalIndent(s.Values, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(s.Path, data, 0o644)
}

func (s *Store) ProgramFor(path string) string {
	ext := strings.ToLower(filepath.Ext(path))
	if program := strings.TrimSpace(s.Values[ext]); program != "" {
		return program
	}
	return strings.TrimSpace(s.Values["*"])
}

func (s *Store) Open(path string) error {
	if program := s.ProgramFor(path); program != "" {
		return exec.Command(program, path).Start()
	}

	switch runtime.GOOS {
	case "windows":
		return exec.Command("rundll32.exe", "url.dll,FileProtocolHandler", path).Start()
	case "darwin":
		return exec.Command("open", path).Start()
	default:
		return exec.Command("xdg-open", path).Start()
	}
}

func Reset(values map[string]string) {
	defaults := Defaults()
	for key := range values {
		delete(values, key)
	}
	for key, value := range defaults {
		values[key] = value
	}
}
