package core

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
)

// Scripts are the search scripts the panel runs, relative to the project root.
var Scripts = map[string]string{
	"boards": "boards/find_boards.py",
	"ru":     "ru/find_ru_jobs.py",
}

// DefaultArgs are the flags of the main examples in boards/README.md and ru/README.md.
var DefaultArgs = map[string]string{
	"boards": "--hn --hn-months 3 --sources all --remote-only --watchlist",
	"ru":     "",
}

// runCommand runs python with args in root, sending each output line to line.
// It returns the full log and the exit code; canceling ctx kills the process group.
func runCommand(ctx context.Context, python, root string, args []string, line func(string)) (string, int, error) {
	cmd := exec.Command(python, args...)
	cmd.Dir = root
	cmd.Env = scriptEnv(root)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	pr, pw := io.Pipe()
	cmd.Stdout, cmd.Stderr = pw, pw
	if err := cmd.Start(); err != nil {
		return "", -1, fmt.Errorf("не удалось запустить %s: %w", python, err)
	}
	stop := make(chan struct{})
	go func() {
		select {
		case <-ctx.Done():
			syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		case <-stop:
		}
	}()
	var mu sync.Mutex
	var log strings.Builder
	done := make(chan struct{})
	go func() {
		sc := bufio.NewScanner(pr)
		sc.Buffer(make([]byte, 64*1024), 4*1024*1024)
		for sc.Scan() {
			t := Redact(root, sc.Text())
			mu.Lock()
			log.WriteString(t + "\n")
			mu.Unlock()
			if line != nil {
				line(t)
			}
		}
		io.Copy(io.Discard, pr)
		close(done)
	}()
	err := cmd.Wait()
	pw.Close()
	<-done
	close(stop)
	code := 0
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			code = ee.ExitCode()
			err = nil
		} else {
			code = -1
		}
	}
	if ctx.Err() != nil {
		err = context.Canceled
	}
	mu.Lock()
	defer mu.Unlock()
	return log.String(), code, err
}

// SplitArgs splits a command line into arguments, with "quotes" for spaces.
func SplitArgs(s string) []string {
	var out []string
	var cur strings.Builder
	var quote rune
	in := false
	for _, r := range s {
		switch {
		case quote != 0:
			if r == quote {
				quote = 0
			} else {
				cur.WriteRune(r)
			}
		case r == '"' || r == '\'':
			quote, in = r, true
		case r == ' ' || r == '\t' || r == '\n':
			if in {
				out = append(out, cur.String())
				cur.Reset()
				in = false
			}
		default:
			cur.WriteRune(r)
			in = true
		}
	}
	if in {
		out = append(out, cur.String())
	}
	return out
}

// ---------- the health of sources, from the scripts' logs ----------

var (
	// ru: "habr            247       7      7  работает"
	ruRow = regexp.MustCompile(`^(\w+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(.+)$`)
	// boards: "remoteok: вакансий 97, подходящих 5" / "remotive: не загрузился (...); пропускаю"
	bSrc    = regexp.MustCompile(`^(remoteok|remotive|himalayas|arbeitnow|jobicy): (?:вакансий (\d+), подходящих (\d+)|(.+))$`)
	bHN     = regexp.MustCompile(`^HN: тема \d+ .*?(?:\((\w+ \d{4})\))? - комментариев (\d+), подходящих компаний (\d+)`)
	bHNFail = regexp.MustCompile(`^HN: (.+)$`)
	bProv   = regexp.MustCompile(`^(greenhouse|lever|ashby): .*бордов существует (\d+).*с подходящими вакансиями (\d+)`)
)

// Health parses what a run's log says about each source.
func Health(script, log string) []SourceHealth {
	out := []SourceHealth{}
	inTable := false
	for _, l := range strings.Split(log, "\n") {
		l = strings.TrimRight(l, " ")
		switch script {
		case "ru":
			if strings.HasPrefix(l, "Источник") {
				inTable = true
				continue
			}
			if inTable {
				m := ruRow.FindStringSubmatch(l)
				if m == nil {
					inTable = false
					continue
				}
				got, _ := strconv.Atoi(m[2])
				passed, _ := strconv.Atoi(m[3])
				st := strings.TrimSpace(m[5])
				ok := st == "работает"
				note := st
				if ok && passed == 0 {
					note = "работает, но 0 после фильтров"
				}
				out = append(out, SourceHealth{Script: script, Source: m[1], Got: got, Passed: passed, OK: ok, Note: note})
			}
		case "boards":
			if m := bSrc.FindStringSubmatch(l); m != nil {
				if m[4] != "" {
					out = append(out, SourceHealth{Script: script, Source: m[1], Note: m[4]})
					continue
				}
				got, _ := strconv.Atoi(m[2])
				passed, _ := strconv.Atoi(m[3])
				note := "работает"
				if passed == 0 {
					note = "работает, но 0 подходящих"
				}
				out = append(out, SourceHealth{Script: script, Source: m[1], Got: got, Passed: passed, OK: true, Note: note})
			} else if m := bHN.FindStringSubmatch(l); m != nil {
				got, _ := strconv.Atoi(m[2])
				passed, _ := strconv.Atoi(m[3])
				name := "HN Who is hiring"
				if m[1] != "" {
					name = "HN · " + m[1]
				}
				out = append(out, SourceHealth{Script: script, Source: name, Got: got, Passed: passed, OK: true,
					Note: "компаний-кандидатов"})
			} else if m := bProv.FindStringSubmatch(l); m != nil {
				got, _ := strconv.Atoi(m[2])
				passed, _ := strconv.Atoi(m[3])
				out = append(out, SourceHealth{Script: script, Source: m[1], Got: got, Passed: passed, OK: true,
					Note: "бордов найдено / с подходящими вакансиями"})
			} else if m := bHNFail.FindStringSubmatch(l); m != nil && strings.Contains(l, "не") && !strings.Contains(l, "тема") {
				out = append(out, SourceHealth{Script: script, Source: "HN Who is hiring", Note: m[1]})
			}
		}
	}
	return out
}

func scriptPath(root, script string) (string, error) {
	p, ok := Scripts[script]
	if !ok {
		return "", fmt.Errorf("неизвестный скрипт %q", script)
	}
	return filepath.Join(root, p), nil
}
