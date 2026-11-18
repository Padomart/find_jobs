package core

import (
	"bufio"
	"context"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// SecretKeys are the variables of .env the panel reports as set or not. Their
// values never leave this package: they go only into the scripts' environment.
var SecretKeys = []string{"JOBSYNC_URL", "JOBSYNC_TOKEN", "DEEPSEEK_API_KEY", "SUPERJOB_KEY"}

// EnvVar is whether a variable of .env has a value. Never the value itself.
type EnvVar struct {
	Name string `json:"name"`
	Set  bool   `json:"set"`
}

// readDotEnv parses .env (KEY=value lines, # comments, optional quotes).
func readDotEnv(root string) map[string]string {
	out := map[string]string{}
	f, err := os.Open(filepath.Join(root, ".env"))
	if err != nil {
		return out
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimPrefix(line, "export ")
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		v = strings.TrimSpace(v)
		if len(v) >= 2 && (v[0] == '"' || v[0] == '\'') && v[len(v)-1] == v[0] {
			v = v[1 : len(v)-1]
		}
		out[strings.TrimSpace(k)] = v
	}
	return out
}

// EnvStatus reports which secrets .env (or the process environment) sets.
func EnvStatus(root string) (vars []EnvVar, fileExists bool) {
	_, err := os.Stat(filepath.Join(root, ".env"))
	fileExists = err == nil
	env := readDotEnv(root)
	for _, k := range SecretKeys {
		v := env[k]
		if v == "" {
			v = os.Getenv(k)
		}
		vars = append(vars, EnvVar{Name: k, Set: strings.TrimSpace(v) != ""})
	}
	return
}

// scriptEnv is the environment for the scripts: the app's own plus .env.
func scriptEnv(root string) []string {
	env := os.Environ()
	for k, v := range readDotEnv(root) {
		env = append(env, k+"="+v)
	}
	env = append(env, "PYTHONUNBUFFERED=1", "PYTHONIOENCODING=utf-8")
	return env
}

// FindPython finds python3 the way the user's shell does: an app started from
// Finder has a short PATH, so ask a login shell first.
func FindPython() string {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if out, err := exec.CommandContext(ctx, "/bin/zsh", "-lc", "command -v python3").Output(); err == nil {
		if p := strings.TrimSpace(string(out)); p != "" {
			return p
		}
	}
	for _, p := range []string{"/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"} {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return "python3"
}

// Versions are the tools the panel uses.
type Versions struct {
	Go     string `json:"go"`
	Python string `json:"python"`
	PyPath string `json:"pythonPath"`
}

// ToolVersions reports the Go the panel was built with and the Python it runs.
func ToolVersions(python string) Versions {
	v := Versions{Go: runtime.Version(), PyPath: python}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if out, err := exec.CommandContext(ctx, python, "--version").CombinedOutput(); err == nil {
		v.Python = strings.TrimSpace(string(out))
	} else {
		v.Python = "не найден: " + err.Error()
	}
	return v
}

// IsProjectRoot says whether dir looks like the find_jobs project.
func IsProjectRoot(dir string) bool {
	for _, f := range []string{"boards/find_boards.py", "ru/find_ru_jobs.py", "common/jobsync_push.py"} {
		if _, err := os.Stat(filepath.Join(dir, f)); err != nil {
			return false
		}
	}
	return true
}

// BlacklistPath is the list of big companies boards skips.
const BlacklistPath = "boards/blacklist.txt"

func ReadBlacklist(root string) (string, error) {
	b, err := os.ReadFile(filepath.Join(root, BlacklistPath))
	if os.IsNotExist(err) {
		return "", nil
	}
	return string(b), err
}

func WriteBlacklist(root, text string) error {
	if !strings.HasSuffix(text, "\n") {
		text += "\n"
	}
	p := filepath.Join(root, BlacklistPath)
	tmp := p + ".tmp"
	if err := os.WriteFile(tmp, []byte(text), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, p)
}

// Redact hides the values of .env's secrets in text (logs, messages), and the
// host of JOBSYNC_URL, which scripts print in network errors.
func Redact(root, text string) string {
	env := readDotEnv(root)
	for _, k := range SecretKeys {
		v := strings.TrimSpace(env[k])
		if v == "" {
			v = strings.TrimSpace(os.Getenv(k))
		}
		if len(v) < 4 {
			continue
		}
		text = strings.ReplaceAll(text, v, "«"+k+"»")
		if k == "JOBSYNC_URL" {
			if u, err := url.Parse(v); err == nil && u.Host != "" {
				text = strings.ReplaceAll(text, u.Host, "«адрес JobSync»")
				if h := u.Hostname(); len(h) >= 4 {
					text = strings.ReplaceAll(text, h, "«адрес JobSync»")
				}
			}
		}
	}
	return text
}
