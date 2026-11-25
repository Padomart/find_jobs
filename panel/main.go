// Панель вакансий: the mygo desktop shell around package core. Everything the
// window can do is a method of Panel; core knows nothing about mygo, so another
// shell (a local site) can wrap the same core.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"log"
	"math"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/egoist/mygo"

	"panel/core"
)

// ---------- events from Go to the page ----------

// RunLine is one line a running script printed.
type RunLine struct {
	RunID  int64  `json:"runId"`
	Script string `json:"script"`
	Text   string `json:"text"`
}

// RunLineEvent streams the scripts' output to the Runs screen.
var RunLineEvent = mygo.NewEvent[RunLine]("run:line")

// RunDoneEvent says a script finished (and its jobs were imported).
var RunDoneEvent = mygo.NewEvent[core.Run]("run:done")

// JobsChangedEvent asks the page to reload the list (import, queue sent).
var JobsChangedEvent = mygo.NewEvent[string]("jobs:changed")

// ---------- the project root, kept outside the project ----------

type config struct {
	Root string `json:"root"`
}

func configPath() string {
	dir, err := os.UserConfigDir()
	if err != nil {
		dir = os.TempDir()
	}
	return filepath.Join(dir, "find-jobs-panel", "config.json")
}

func loadConfig() config {
	var c config
	if b, err := os.ReadFile(configPath()); err == nil {
		json.Unmarshal(b, &c)
	}
	return c
}

func saveConfig(c config) error {
	os.MkdirAll(filepath.Dir(configPath()), 0o755)
	b, _ := json.MarshalIndent(c, "", "  ")
	return os.WriteFile(configPath(), b, 0o644)
}

// guessRoot finds the project: the saved path, FINDJOBS_ROOT, a parent of the
// working directory or of the executable, or ~/Documents/Work/find_jobs.
func guessRoot() string {
	if c := loadConfig(); c.Root != "" && core.IsProjectRoot(c.Root) {
		return c.Root
	}
	if r := os.Getenv("FINDJOBS_ROOT"); r != "" && core.IsProjectRoot(r) {
		return r
	}
	var starts []string
	if wd, err := os.Getwd(); err == nil {
		starts = append(starts, wd)
	}
	if exe, err := os.Executable(); err == nil {
		starts = append(starts, filepath.Dir(exe))
	}
	for _, d := range starts {
		for ; d != filepath.Dir(d); d = filepath.Dir(d) {
			if core.IsProjectRoot(d) {
				return d
			}
		}
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, "Documents", "Work", "find_jobs")
}

// ---------- the service the page calls ----------

// Panel is what the page calls. Every method returns errors in Russian, with
// what to do about them.
type Panel struct {
	mu   sync.Mutex
	app  *core.App
	err  error // why the project could not be opened
	win  *mygo.Window
	tray *mygo.Tray
}

func (p *Panel) open(root string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.app != nil {
		p.app.Close()
		p.app = nil
	}
	a, err := core.NewApp(root)
	p.app, p.err = a, err
	if err == nil {
		saveConfig(config{Root: root})
		a.Import()
	}
}

func (p *Panel) core() (*core.App, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.app == nil {
		if p.err != nil {
			return nil, p.err
		}
		return nil, errors.New("проект не открыт — укажите папку find_jobs в Настройках")
	}
	return p.app, nil
}

// Status is the state the page starts from.
type Status struct {
	Ready bool   `json:"ready"`
	Root  string `json:"root"`
	Error string `json:"error,omitempty"`
	// StartView is the screen to open on (PANEL_VIEW=overview|runs|settings), for screenshots.
	StartView string `json:"startView,omitempty"`
}

// Status says whether the project is open, and why not.
func (p *Panel) Status() Status {
	p.mu.Lock()
	defer p.mu.Unlock()
	s := Status{Ready: p.app != nil, StartView: os.Getenv("PANEL_VIEW")}
	if p.app != nil {
		s.Root = p.app.Root
	} else {
		s.Root = guessRoot()
	}
	if p.err != nil {
		s.Error = p.err.Error()
	}
	return s
}

// ChooseRoot asks for the find_jobs folder and opens it. Returns "" when canceled.
func (p *Panel) ChooseRoot(ctx context.Context) (string, error) {
	paths, err := mygo.Dialog.Open(mygo.OpenDialogOptions{Parent: mygo.CallerWindow(ctx), Directory: true,
		Title: "Папка проекта find_jobs", ButtonLabel: "Выбрать"})
	if err != nil || len(paths) == 0 {
		return "", err
	}
	return paths[0], p.SetRoot(paths[0])
}

// SetRoot opens the project at root.
func (p *Panel) SetRoot(root string) error {
	if !core.IsProjectRoot(root) {
		return fmt.Errorf("в %s нет en/find_boards.py, ru/find_ru_jobs.py и common/jobsync_push.py — "+
			"выберите корень проекта find_jobs", root)
	}
	p.open(root)
	p.refreshBadge()
	return p.err
}

// List returns a page of the inbox.
func (p *Panel) List(f core.Filter) (core.Page, error) {
	a, err := p.core()
	if err != nil {
		return core.Page{}, err
	}
	return a.Store.List(f)
}

// Get returns one job.
func (p *Panel) Get(id int64) (core.Job, error) {
	a, err := p.core()
	if err != nil {
		return core.Job{}, err
	}
	return a.Store.Get(id)
}

// Sources lists the sources for the filter.
func (p *Panel) Sources() ([]string, error) {
	a, err := p.core()
	if err != nil {
		return nil, err
	}
	return a.Store.Sources()
}

// Counts returns the jobs per status.
func (p *Panel) Counts() (core.Counts, error) {
	a, err := p.core()
	if err != nil {
		return core.Counts{}, err
	}
	return a.Store.Counts()
}

// Act moves jobs to a status: shortlisted (оставить), dismissed (в корзину) or new (вернуть).
func (p *Panel) Act(ids []int64, status core.Status) (int, error) {
	a, err := p.core()
	if err != nil {
		return 0, err
	}
	n, err := a.Act(ids, status)
	p.refreshBadge()
	return n, err
}

// Undo reverts the last Act.
func (p *Panel) Undo() (int, error) {
	a, err := p.core()
	if err != nil {
		return 0, err
	}
	n, err := a.Undo()
	p.refreshBadge()
	return n, err
}

// SaveEdit stores the user's edits of a job.
func (p *Panel) SaveEdit(id int64, e core.Edit) (core.Job, error) {
	a, err := p.core()
	if err != nil {
		return core.Job{}, err
	}
	return a.Store.SaveEdit(id, e)
}

// Send sends jobs to JobSync; resend updates jobs already sent.
func (p *Panel) Send(ctx context.Context, ids []int64, resend bool) (core.SendResult, error) {
	a, err := p.core()
	if err != nil {
		return core.SendResult{}, err
	}
	r, err := a.Send(ctx, ids, resend)
	p.refreshBadge()
	return r, err
}

// Budget is what is left of the JobSync limit this hour.
func (p *Panel) Budget() (core.Budget, error) {
	a, err := p.core()
	if err != nil {
		return core.Budget{}, err
	}
	return core.ReadBudget(a.Root)
}

// Import imports the scripts' result files again.
func (p *Panel) Import() ([]core.ImportResult, error) {
	a, err := p.core()
	if err != nil {
		return nil, err
	}
	r, err := a.Import()
	p.refreshBadge()
	return r, err
}

// EmptyTrash deletes the dismissed jobs for good.
func (p *Panel) EmptyTrash() (int, error) {
	a, err := p.core()
	if err != nil {
		return 0, err
	}
	return a.Store.EmptyTrash()
}

// Summary is the overview screen.
func (p *Panel) Summary() (core.Summary, error) {
	a, err := p.core()
	if err != nil {
		return core.Summary{}, err
	}
	return a.Summary()
}

// Runs lists the latest runs.
func (p *Panel) Runs() ([]core.Run, error) {
	a, err := p.core()
	if err != nil {
		return nil, err
	}
	return a.Store.Runs(50)
}

// RunLog returns a run with its log.
func (p *Panel) RunLog(id int64) (core.Run, error) {
	a, err := p.core()
	if err != nil {
		return core.Run{}, err
	}
	return a.Store.RunLog(id)
}

// StartRun starts a search script; its lines arrive as run:line events and
// its end as run:done, after the new jobs were imported.
func (p *Panel) StartRun(script string) error {
	a, err := p.core()
	if err != nil {
		return err
	}
	for _, s := range a.Running() {
		if s == script {
			return fmt.Errorf("%s уже запущен", script)
		}
	}
	go func() {
		r, err := a.RunScript(context.Background(), script, func(id int64, text string) {
			RunLineEvent.Broadcast(RunLine{RunID: id, Script: script, Text: text})
		})
		if err != nil {
			RunLineEvent.Broadcast(RunLine{Script: script, Text: "ошибка: " + err.Error()})
			r = core.Run{Script: script, Status: "failed", Log: err.Error()}
		}
		RunDoneEvent.Broadcast(r)
		JobsChangedEvent.Broadcast(script)
		p.refreshBadge()
		p.notify(r)
	}()
	return nil
}

// CancelRun stops a running script.
func (p *Panel) CancelRun(script string) bool {
	a, err := p.core()
	if err != nil {
		return false
	}
	return a.Cancel(script)
}

// Running lists the running scripts.
func (p *Panel) Running() []string {
	a, err := p.core()
	if err != nil {
		return nil
	}
	return a.Running()
}

// Settings returns the settings screen's data: secrets only as set / not set.
func (p *Panel) Settings() (core.Settings, error) {
	a, err := p.core()
	if err != nil {
		return core.Settings{Root: guessRoot()}, err
	}
	return a.Settings(), nil
}

// SaveSettings stores the scripts' flags, the Python path and the duplicate check.
func (p *Panel) SaveSettings(s core.Settings) error {
	a, err := p.core()
	if err != nil {
		return err
	}
	return a.SaveSettings(s)
}

// SaveBlacklist writes en/blacklist.txt.
func (p *Panel) SaveBlacklist(text string) error {
	a, err := p.core()
	if err != nil {
		return err
	}
	return a.SaveBlacklist(text)
}

// OpenURL opens a job's original posting in the default browser.
func (p *Panel) OpenURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") {
		return fmt.Errorf("это не ссылка на сайт: %s", raw)
	}
	return mygo.Shell.OpenExternal(u.String())
}

// ---------- tray, Dock badge, notifications ----------

// trayIcon draws a small briefcase as a template image (black on transparent).
func trayIcon() []byte {
	const n = 36
	img := image.NewNRGBA(image.Rect(0, 0, n, n))
	ink := color.NRGBA{0, 0, 0, 255}
	in := func(x, y, x0, y0, x1, y1, r float64) bool {
		cx, cy := math.Max(x0+r, math.Min(x, x1-r)), math.Max(y0+r, math.Min(y, y1-r))
		return (x-cx)*(x-cx)+(y-cy)*(y-cy) <= r*r
	}
	for y := 0; y < n; y++ {
		for x := 0; x < n; x++ {
			fx, fy := float64(x)+0.5, float64(y)+0.5
			body := in(fx, fy, 3, 11, 33, 31, 4) && !in(fx, fy, 6, 14, 30, 28, 2)
			handle := in(fx, fy, 12, 5, 24, 13, 3) && !in(fx, fy, 15, 8, 21, 13, 1)
			band := fy >= 18 && fy <= 21 && fx >= 4 && fx <= 32
			if body || handle || band {
				img.Set(x, y, ink)
			}
		}
	}
	var buf bytes.Buffer
	png.Encode(&buf, img)
	return buf.Bytes()
}

func (p *Panel) refreshBadge() {
	a, err := p.core()
	if err != nil {
		return
	}
	c, err := a.Store.Counts()
	if err != nil {
		return
	}
	title := ""
	if c.New > 0 {
		title = fmt.Sprint(c.New)
	}
	if p.tray != nil {
		p.tray.SetTitle(title)
		p.tray.SetToolTip(fmt.Sprintf("Панель вакансий: новых %d, оставлено %d", c.New, c.Shortlisted))
	}
	mygo.App.SetBadgeCount(c.New)
}

func (p *Panel) notify(r core.Run) {
	if !mygo.NotificationsSupported() {
		return
	}
	name := map[string]string{"boards": "Поиск бордов", "ru": "Поиск RU"}[r.Script]
	body := fmt.Sprintf("Новых вакансий: %d", r.Imported)
	switch r.Status {
	case "failed":
		body = "Завершился с ошибкой — подробности в журнале запусков"
	case "canceled":
		body = "Остановлен"
	}
	n := mygo.NewNotification(mygo.NotificationOptions{Title: name, Body: body})
	n.OnClick(func() {
		if p.win != nil {
			p.win.Show()
			p.win.Focus()
		}
		n.Close()
	})
	go n.Show()
}

func (p *Panel) showWindow() {
	if p.win != nil {
		p.win.Show()
		p.win.Focus()
	}
}

func main() {
	p := &Panel{}
	mygo.Bind(p)
	mygo.App.WhenReady(func() {
		p.open(guessRoot())
		p.win = mygo.NewWindow(mygo.WindowOptions{
			Title:           "Панель вакансий",
			Width:           1280,
			Height:          820,
			MinWidth:        900,
			MinHeight:       560,
			BackgroundColor: "light-dark(#f5f5f7, #1e1e20)",
			TitleBarStyle:   mygo.TitleBarHiddenInset,
			Transparent:     true,
			Vibrancy:        mygo.VibrancySidebar,
			StateKey:        "main",
			URL:             "/",
		})
		tray, err := mygo.NewTray(mygo.TrayOptions{
			ID:             "panel",
			Icon:           trayIcon(),
			IconIsTemplate: true,
			ToolTip:        "Панель вакансий",
			Menu: mygo.NewMenu([]*mygo.MenuItem{
				{Label: "Открыть панель", Click: func(*mygo.MenuItem, *mygo.Window) { p.showWindow() }},
				{Label: "Запустить поиск бордов", Click: func(*mygo.MenuItem, *mygo.Window) { p.StartRun("boards") }},
				{Label: "Запустить поиск RU", Click: func(*mygo.MenuItem, *mygo.Window) { p.StartRun("ru") }},
				mygo.Separator(),
				{Role: mygo.RoleQuit},
			}),
		})
		if err != nil {
			log.Println("tray:", err)
		} else {
			p.tray = tray
		}
		p.refreshBadge()
		mygo.App.OnDidBecomeActive(func() { mygo.ClearNotifications() })
		// the JobSync queue: send what waits once the hour's budget frees up
		go func() {
			for {
				time.Sleep(2 * time.Minute)
				a, err := p.core()
				if err != nil {
					continue
				}
				r, err := a.ProcessQueue(context.Background())
				if r != nil || err != nil {
					JobsChangedEvent.Broadcast("queue")
					p.refreshBadge()
					if r != nil && len(r.Sent) > 0 && mygo.NotificationsSupported() {
						go mygo.NewNotification(mygo.NotificationOptions{Title: "JobSync",
							Body: "Из очереди: " + strings.TrimSpace(r.Message)}).Show()
					}
				}
			}
		}()
	})
	mygo.App.OnWindowAllClosed(func() {}) // stays in the tray; quit from its menu or ⌘Q
	if err := mygo.App.Run(); err != nil {
		log.Fatal(err)
	}
}
