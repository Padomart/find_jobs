package core

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// project copies testdata/project to a temp dir (the root .gitignore ignores
// every state/ directory, so the ledger fixture lives beside it).
func project(t *testing.T) (root string, a *App) {
	t.Helper()
	root = t.TempDir()
	err := filepath.WalkDir("testdata/project", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel("testdata/project", p)
		dst := filepath.Join(root, rel)
		if d.IsDir() {
			return os.MkdirAll(dst, 0o755)
		}
		b, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		return os.WriteFile(dst, b, 0o644)
	})
	if err != nil {
		t.Fatal(err)
	}
	led, _ := os.ReadFile("testdata/ledger.json")
	os.MkdirAll(filepath.Join(root, "state"), 0o755)
	os.WriteFile(filepath.Join(root, LedgerFile), led, 0o644)
	a, err = NewApp(root)
	if err != nil {
		t.Fatal(err)
	}
	a.Python = "python3"
	t.Cleanup(func() { a.Close() })
	return root, a
}

func all(t *testing.T, a *App) []Job {
	t.Helper()
	p, err := a.Store.List(Filter{Status: "all", Limit: 1000})
	if err != nil {
		t.Fatal(err)
	}
	return p.Jobs
}

func byURL(t *testing.T, a *App, url string) Job {
	t.Helper()
	j, err := a.Store.GetByURL(url)
	if err != nil {
		t.Fatalf("%s: %v", url, err)
	}
	return j
}

func TestImportCollapsesDuplicatesAndReadsAllFormats(t *testing.T) {
	_, a := project(t)
	res, err := a.Import()
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]ImportResult{}
	for _, r := range res {
		if r.Error != "" {
			t.Fatalf("%s: %s", r.File, r.Error)
		}
		got[filepath.Base(r.File)] = r
	}
	if r := got["jobs_noats.json"]; r.Read != 3 || r.Dupes != 1 || r.Added != 2 {
		t.Errorf("noats: %+v, want 3 read, 1 dupe, 2 added", r)
	}
	if r := got["boards.json"]; r.Added != 1 {
		t.Errorf("boards: %+v, want 1 added", r)
	}
	if r := got["jobs_ru.json"]; r.Added != 2 {
		t.Errorf("ru: %+v, want 2 added", r)
	}
	if n := len(all(t, a)); n != 5 {
		t.Fatalf("%d jobs, want 5", n)
	}
	ask := byURL(t, a, "https://remotive.com/remote-jobs/x/askable-1")
	if !strings.HasPrefix(ask.Description, "A much longer") {
		t.Errorf("the fuller duplicate should win, got %q", ask.Description)
	}
	if ask.Score == nil || *ask.Score != 6 || ask.ScoreKind != "ai" || ask.AI == nil || ask.AI.Reason != "ok" {
		t.Errorf("AI score lost: %+v %+v", ask.Score, ask.AI)
	}
	mir := byURL(t, a, "https://remoteok.com/remote-jobs/mirantis-2")
	if mir.Years != 3 || mir.Go || !strings.Contains(strings.Join(mir.Flags, "|"), "ограничение: PST hours") {
		t.Errorf("mirantis: %+v", mir)
	}
	ru := byURL(t, a, "https://t.me/c/2")
	if ru.Market != "ru" || ru.Source != "Telegram" || ru.ScoreKind != "heuristic" ||
		!strings.Contains(strings.Join(ru.Flags, "|"), "РФ: только РФ") {
		t.Errorf("ru job: %+v", ru)
	}
	if b := byURL(t, a, "https://job-boards.greenhouse.io/enveritas/jobs/1"); b.Company != "Enveritas" ||
		b.Source != "Greenhouse" || !b.Python {
		t.Errorf("board job: %+v", b)
	}
}

func TestLedgerMarksSent(t *testing.T) {
	_, a := project(t)
	a.Import()
	j := byURL(t, a, "https://career.habr.com/vacancies/1")
	if j.Status != StatusSent || j.SentAt != "2026-11-14T18:22:05+00:00" {
		t.Fatalf("ledger job: status %s sent_at %q", j.Status, j.SentAt)
	}
}

func TestReimportIsIdempotentAndKeepsUserState(t *testing.T) {
	_, a := project(t)
	a.Import()
	ask := byURL(t, a, "https://remotive.com/remote-jobs/x/askable-1")
	mir := byURL(t, a, "https://remoteok.com/remote-jobs/mirantis-2")
	if _, err := a.Act([]int64{ask.ID}, StatusShortlisted); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Act([]int64{mir.ID}, StatusDismissed); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Store.SaveEdit(ask.ID, Edit{Title: "Мой заголовок", Company: ask.Company,
		Description: "Моё описание", Notes: "позвонить"}); err != nil {
		t.Fatal(err)
	}

	res, err := a.Import()
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range res {
		if r.Added != 0 {
			t.Errorf("%s: second import added %d", r.File, r.Added)
		}
	}
	if n := len(all(t, a)); n != 5 {
		t.Fatalf("%d jobs after reimport, want 5", n)
	}
	ask = byURL(t, a, ask.URL)
	if ask.Status != StatusShortlisted || ask.EditTitle != "Мой заголовок" || ask.Notes != "позвонить" ||
		ask.EditDesc != "Моё описание" || ask.EditCompany != "" {
		t.Errorf("user state lost on reimport: %+v", ask)
	}
	if ask.Title != "Software Engineer, AI Coding Tasks" || ask.EffTitle() != "Мой заголовок" {
		t.Errorf("original/effective title: %q / %q", ask.Title, ask.EffTitle())
	}
	if byURL(t, a, mir.URL).Status != StatusDismissed {
		t.Error("reimport brought a dismissed job back")
	}
}

func TestDismissUndoRestoreAndEmptyTrash(t *testing.T) {
	_, a := project(t)
	a.Import()
	j := byURL(t, a, "https://remoteok.com/remote-jobs/mirantis-2")
	sent := byURL(t, a, "https://career.habr.com/vacancies/1")
	if _, err := a.Act([]int64{j.ID, sent.ID}, StatusDismissed); err != nil {
		t.Fatal(err)
	}
	p, _ := a.Store.List(Filter{})
	for _, x := range p.Jobs {
		if x.Status == StatusDismissed {
			t.Fatal("the default list shows the trash")
		}
	}
	if n, err := a.Undo(); err != nil || n != 2 {
		t.Fatalf("undo: %d %v", n, err)
	}
	if byURL(t, a, j.URL).Status != StatusNew || byURL(t, a, sent.URL).Status != StatusSent {
		t.Fatal("undo did not restore the statuses")
	}
	// "Вернуть" a sent job from the trash keeps it sent
	a.Act([]int64{sent.ID}, StatusDismissed)
	a.Act([]int64{sent.ID}, StatusNew)
	if byURL(t, a, sent.URL).Status != StatusSent {
		t.Error("restoring a sent job lost the sent status")
	}
	if _, err := a.Act([]int64{j.ID}, StatusSent); err == nil {
		t.Error("setting sent by hand must fail")
	}
	a.Act([]int64{j.ID}, StatusDismissed)
	// the ledger must not pull a dismissed job out of the trash
	a.Act([]int64{sent.ID}, StatusDismissed)
	a.Store.SyncLedger(a.Root)
	if byURL(t, a, sent.URL).Status != StatusDismissed {
		t.Error("ledger sync resurrected a dismissed job")
	}
	n, err := a.Store.EmptyTrash()
	if err != nil || n != 2 {
		t.Fatalf("empty trash: %d %v", n, err)
	}
	if len(all(t, a)) != 3 {
		t.Fatal("empty trash removed the wrong jobs")
	}
}

func TestListFilters(t *testing.T) {
	_, a := project(t)
	a.Import()
	cases := []struct {
		f    Filter
		want int
	}{
		{Filter{Market: "ru"}, 2},
		{Filter{Market: "world"}, 3},
		{Filter{Lang: "python"}, 1},
		{Filter{MinScore: 6.5}, 2},
		{Filter{Query: "askable"}, 1},
		{Filter{Query: "удалённо рф"}, 1},
		{Filter{Source: "Remote OK"}, 1},
		{Filter{Status: "sent"}, 1},
	}
	for _, c := range cases {
		p, err := a.Store.List(c.f)
		if err != nil {
			t.Fatal(err)
		}
		if p.Total != c.want || len(p.Jobs) != c.want {
			t.Errorf("%+v: total %d, %d jobs, want %d", c.f, p.Total, len(p.Jobs), c.want)
		}
	}
	p, _ := a.Store.List(Filter{Status: "all", Sort: "score", Limit: 2, Offset: 0})
	if p.Total != 5 || len(p.Jobs) != 2 || *p.Jobs[0].Score < *p.Jobs[1].Score {
		t.Errorf("paging/sorting: %+v", p)
	}
}

func TestOutboxUsesEdits(t *testing.T) {
	_, a := project(t)
	a.Import()
	j := byURL(t, a, "https://t.me/c/2")
	j, _ = a.Store.SaveEdit(j.ID, Edit{Title: j.Title, Company: "Yandex Cloud", Description: "Полный текст"})
	m := outboxItem(j)
	if m["company"] != "Yandex Cloud" || m["description"] != "Полный текст" || m["source"] != "telegram" {
		t.Errorf("ru outbox item: %+v", m)
	}
	if _, ok := m["text"]; ok {
		t.Error("ru item must carry description, not text (jobsync_push picks the format by it)")
	}
	b := outboxItem(byURL(t, a, "https://remoteok.com/remote-jobs/mirantis-2"))
	if b["text"] == nil || b["restrict"] != "PST hours" || b["visa"] != true || b["text_incomplete"] != true {
		t.Errorf("boards outbox item: %+v", b)
	}
}

func TestHealthAndArgs(t *testing.T) {
	ru := "Источник   получено  прошло  новых  статус\nhabr            247       7      7  работает\n" +
		"superjob          0       0      0  пропущен: нет SUPERJOB_KEY\nmts 16 0 0  работает\n  0 из mts: ..."
	h := Health("ru", ru)
	if len(h) != 3 || !h[0].OK || h[1].OK || h[2].Note != "работает, но 0 после фильтров" {
		t.Errorf("ru health: %+v", h)
	}
	b := "remoteok: вакансий 97, подходящих 5\nremotive: не загрузился (HTTP 429); пропускаю\n" +
		"HN: тема 50338728 Ask HN: Who is hiring? (November 2026) - комментариев 243, подходящих компаний 96 (с прямой ссылкой на ATS: 16)\n" +
		"lever: прямых ссылок 4, бордов существует 6 (крупных 0, в чёрном списке 0), чужих 0, нет борда 0, с подходящими вакансиями 0"
	h = Health("boards", b)
	if len(h) != 4 || h[0].Passed != 5 || h[1].OK || h[2].Got != 243 || h[3].Source != "lever" || h[3].Passed != 0 {
		t.Errorf("boards health: %+v", h)
	}
	if got := SplitArgs(`--hn --query "go dev" -v`); strings.Join(got, "|") != "--hn|--query|go dev|-v" {
		t.Errorf("SplitArgs: %q", got)
	}
}

func TestEnvStatusNeverReturnsValues(t *testing.T) {
	root, _ := project(t)
	os.WriteFile(filepath.Join(root, ".env"), []byte("JOBSYNC_URL=https://secret.example\nJOBSYNC_TOKEN=\"tok\"\n# c\n"), 0o600)
	vars, ok := EnvStatus(root)
	if !ok {
		t.Fatal(".env not seen")
	}
	set := map[string]bool{}
	for _, v := range vars {
		set[v.Name] = v.Set
	}
	if !set["JOBSYNC_URL"] || !set["JOBSYNC_TOKEN"] || set["SUPERJOB_KEY"] && os.Getenv("SUPERJOB_KEY") == "" {
		t.Errorf("env status: %+v", vars)
	}
}

func TestRedactHidesSecretsAndHost(t *testing.T) {
	root, _ := project(t)
	os.WriteFile(filepath.Join(root, ".env"), []byte("JOBSYNC_URL=http://jobs.internal:9999\nJOBSYNC_TOKEN=abcd1234secret\n"), 0o600)
	got := Redact(root, "JobSync недоступен: jobs.internal:9999: refused; Bearer abcd1234secret; http://jobs.internal:9999/api/mcp")
	for _, leak := range []string{"jobs.internal", "9999", "abcd1234secret"} {
		if strings.Contains(got, leak) {
			t.Errorf("leaked %q: %s", leak, got)
		}
	}
}
