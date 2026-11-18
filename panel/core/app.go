package core

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// App ties the store to the project: what both shells (mygo window, local
// site) call. It is safe for concurrent use.
type App struct {
	Root   string
	Python string
	Store  *Store

	mu      sync.Mutex
	undo    [][]Prior // last status changes, for undo
	sendMu  sync.Mutex
	runMu   sync.Mutex
	running map[string]context.CancelFunc // script -> cancel
}

// DBPath is where the panel keeps its database, under the git-ignored state/.
func DBPath(root string) string { return filepath.Join(root, "state", "panel", "panel.db") }

// NewApp opens the panel for the project at root.
func NewApp(root string) (*App, error) {
	if !IsProjectRoot(root) {
		return nil, fmt.Errorf("%s не похоже на папку find_jobs (нет boards/find_boards.py, ru/find_ru_jobs.py "+
			"или common/jobsync_push.py) — укажите путь в Настройках", root)
	}
	st, err := OpenStore(DBPath(root))
	if err != nil {
		return nil, err
	}
	a := &App{Root: root, Store: st, running: map[string]context.CancelFunc{}}
	a.Python = st.Setting("python", "")
	if a.Python == "" {
		a.Python = FindPython()
	}
	return a, nil
}

func (a *App) Close() error { return a.Store.Close() }

// Import imports the scripts' files and syncs the JobSync ledger.
func (a *App) Import() ([]ImportResult, error) { return a.Store.ImportAll(a.Root) }

// Act changes the status of jobs and remembers the change for Undo.
func (a *App) Act(ids []int64, st Status) (int, error) {
	prior, err := a.Store.SetStatus(ids, st)
	if err != nil {
		return 0, err
	}
	a.mu.Lock()
	a.undo = append(a.undo, prior)
	if len(a.undo) > 50 {
		a.undo = a.undo[1:]
	}
	a.mu.Unlock()
	return len(prior), nil
}

// Undo reverts the last Act. It returns how many jobs it put back.
func (a *App) Undo() (int, error) {
	a.mu.Lock()
	if len(a.undo) == 0 {
		a.mu.Unlock()
		return 0, errors.New("нечего отменять")
	}
	prior := a.undo[len(a.undo)-1]
	a.undo = a.undo[:len(a.undo)-1]
	a.mu.Unlock()
	return len(prior), a.Store.Restore(prior)
}

// Send sends jobs to JobSync (one send at a time: they share the budget).
func (a *App) Send(ctx context.Context, ids []int64, resend bool) (SendResult, error) {
	a.sendMu.Lock()
	defer a.sendMu.Unlock()
	check := a.Store.Setting("check_existing", "1") == "1"
	return Send(ctx, a.Store, a.Root, a.Python, ids, check, resend)
}

// ProcessQueue sends what is queued once the budget allows. It returns the
// result, or nil when nothing was sent.
func (a *App) ProcessQueue(ctx context.Context) (*SendResult, error) {
	ids, err := a.Store.QueuedIDs()
	if err != nil || len(ids) == 0 {
		return nil, err
	}
	b, err := ReadBudget(a.Root)
	if err != nil {
		return nil, err
	}
	check := a.Store.Setting("check_existing", "1") == "1"
	if b.Remaining < SendCost(1, check, false) {
		return nil, nil
	}
	a.Store.SetQueued(ids, false)
	r, err := a.Send(ctx, ids, false)
	return &r, err
}

// ScriptArgs are the flags a script runs with: the settings, else the README's.
func (a *App) ScriptArgs(script string) string {
	return a.Store.Setting("args_"+script, DefaultArgs[script])
}

// Running says which scripts are running.
func (a *App) Running() []string {
	a.runMu.Lock()
	defer a.runMu.Unlock()
	var out []string
	for k := range a.running {
		out = append(out, k)
	}
	return out
}

// RunScript runs a search script, sending its lines to line, records the run,
// and imports the results when it succeeds.
func (a *App) RunScript(ctx context.Context, script string, line func(runID int64, text string)) (Run, error) {
	path, err := scriptPath(a.Root, script)
	if err != nil {
		return Run{}, err
	}
	a.runMu.Lock()
	if _, busy := a.running[script]; busy {
		a.runMu.Unlock()
		return Run{}, fmt.Errorf("%s уже запущен", script)
	}
	ctx, cancel := context.WithCancel(ctx)
	a.running[script] = cancel
	a.runMu.Unlock()
	defer func() {
		a.runMu.Lock()
		delete(a.running, script)
		a.runMu.Unlock()
		cancel()
	}()

	argStr := a.ScriptArgs(script)
	id, err := a.Store.StartRun(script, argStr)
	if err != nil {
		return Run{}, err
	}
	args := append([]string{path}, SplitArgs(argStr)...)
	if line != nil {
		line(id, fmt.Sprintf("$ %s %s %s", filepath.Base(a.Python), Scripts[script], argStr))
	}
	log, code, runErr := runCommand(ctx, a.Python, a.Root, args, func(t string) {
		if line != nil {
			line(id, t)
		}
	})
	status := "ok"
	switch {
	case errors.Is(runErr, context.Canceled):
		status = "canceled"
	case runErr != nil:
		status = "failed"
		log += "\n" + runErr.Error()
	case code != 0:
		status = "failed"
	}
	imported, updated := 0, 0
	if status == "ok" {
		res, err := a.Import()
		for _, r := range res {
			imported += r.Added
			updated += r.Updated
		}
		if err != nil {
			log += "\nимпорт: " + err.Error()
		}
		log += fmt.Sprintf("\n[панель] импортировано новых вакансий: %d, обновлено: %d", imported, updated)
		if line != nil {
			line(id, fmt.Sprintf("[панель] импортировано новых вакансий: %d, обновлено: %d", imported, updated))
		}
	}
	var c *int
	if runErr == nil || status == "canceled" {
		c = &code
	}
	if err := a.Store.FinishRun(id, status, c, log, imported, updated); err != nil {
		return Run{}, err
	}
	return a.Store.RunLog(id)
}

// Cancel stops a running script.
func (a *App) Cancel(script string) bool {
	a.runMu.Lock()
	defer a.runMu.Unlock()
	if c, ok := a.running[script]; ok {
		c()
		return true
	}
	return false
}

// Summary is the overview screen.
type Summary struct {
	Counts  Counts         `json:"counts"`
	Budget  Budget         `json:"budget"`
	Runs    []Run          `json:"runs"`
	Health  []SourceHealth `json:"health"`
	Running []string       `json:"running"`
}

func (a *App) Summary() (Summary, error) {
	var s Summary
	var err error
	if s.Counts, err = a.Store.Counts(); err != nil {
		return s, err
	}
	s.Budget, _ = ReadBudget(a.Root)
	if s.Runs, err = a.Store.Runs(10); err != nil {
		return s, err
	}
	s.Health = []SourceHealth{}
	for _, script := range []string{"boards", "ru"} {
		if r, err := a.Store.LastRunLog(script); err == nil {
			s.Health = append(s.Health, Health(script, r.Log)...)
		}
	}
	s.Running = a.Running()
	return s, nil
}

// Settings are what the settings screen edits and shows.
type Settings struct {
	Root          string   `json:"root"`
	Python        string   `json:"python"`
	ArgsBoards    string   `json:"argsBoards"`
	ArgsRu        string   `json:"argsRu"`
	CheckExisting bool     `json:"checkExisting"`
	EnvFile       bool     `json:"envFile"`
	Env           []EnvVar `json:"env"`
	Versions      Versions `json:"versions"`
	Blacklist     string   `json:"blacklist"`
	DefaultBoards string   `json:"defaultBoards"`
	DefaultRu     string   `json:"defaultRu"`
}

func (a *App) Settings() Settings {
	env, ok := EnvStatus(a.Root)
	bl, _ := ReadBlacklist(a.Root)
	return Settings{Root: a.Root, Python: a.Python, ArgsBoards: a.ScriptArgs("boards"), ArgsRu: a.ScriptArgs("ru"),
		CheckExisting: a.Store.Setting("check_existing", "1") == "1", EnvFile: ok, Env: env,
		Versions: ToolVersions(a.Python), Blacklist: bl,
		DefaultBoards: DefaultArgs["boards"], DefaultRu: DefaultArgs["ru"]}
}

// SaveSettings stores the editable settings (not the project root: the shell keeps that).
func (a *App) SaveSettings(s Settings) error {
	for k, v := range map[string]string{"args_boards": strings.TrimSpace(s.ArgsBoards),
		"args_ru": strings.TrimSpace(s.ArgsRu), "python": strings.TrimSpace(s.Python)} {
		if err := a.Store.SetSetting(k, v); err != nil {
			return err
		}
	}
	check := "0"
	if s.CheckExisting {
		check = "1"
	}
	if err := a.Store.SetSetting("check_existing", check); err != nil {
		return err
	}
	if p := strings.TrimSpace(s.Python); p != "" {
		a.Python = p
	} else {
		a.Python = FindPython()
	}
	return nil
}

// SaveBlacklist writes boards/blacklist.txt.
func (a *App) SaveBlacklist(text string) error { return WriteBlacklist(a.Root, text) }

// QueueLoop sends the queue whenever the budget frees up, until ctx ends.
func (a *App) QueueLoop(ctx context.Context, every time.Duration, done func(*SendResult, error)) {
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			r, err := a.ProcessQueue(ctx)
			if (r != nil || err != nil) && done != nil {
				done(r, err)
			}
		}
	}
}
