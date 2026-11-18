package core

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// The JobSync limits: 60 tool calls an hour for every token, of which
// jobsync_push.py spends at most --budget (50 by default, a margin for the user).
const (
	JobSyncHardLimit = 60
	JobSyncBudget    = 50
	PushScript       = "common/jobsync_push.py"
)

// Budget is what is left of the JobSync limit this hour, by the ledger.
type Budget struct {
	Used      int `json:"used"`
	Budget    int `json:"budget"`
	Remaining int `json:"remaining"`
	HardLimit int `json:"hardLimit"`
	ResetMin  int `json:"resetInMinutes"` // when the oldest unit of the hour frees up
}

// ReadBudget counts the units of the last hour in the ledger.
func ReadBudget(root string) (Budget, error) {
	b := Budget{Budget: JobSyncBudget, HardLimit: JobSyncHardLimit}
	l, err := ReadLedger(root)
	if err != nil {
		return b, err
	}
	nowS := float64(time.Now().UnixNano()) / 1e9
	oldest := math.Inf(1)
	for _, t := range l.Units {
		if nowS-t < 3600 {
			b.Used++
			oldest = math.Min(oldest, t)
		}
	}
	b.Remaining = max(0, b.Budget-b.Used)
	if b.Used > 0 {
		b.ResetMin = int(math.Ceil((3600 - (nowS - oldest)) / 60))
	}
	return b, nil
}

// SendCost is the budget a send takes: find_job and add_jobs_batch per job, or
// add_jobs_batch alone for a resend (upsert updates the existing record).
func SendCost(n int, checkExisting, resend bool) int {
	if checkExisting && !resend {
		return 2 * n
	}
	return n
}

// rawSource maps the names the panel shows back to the scripts' own.
func rawSource(s string) string {
	for k, v := range sourceNames {
		if v == s {
			return k
		}
	}
	return s
}

// outboxItem turns a job, with the user's edits, into the format of the file
// the job came from, which common/jobsync_push.py reads (noats_item).
func outboxItem(j Job) map[string]any {
	var ai map[string]any
	if j.AI != nil {
		ai = map[string]any{"score": j.AI.Score, "reason": j.AI.Reason}
	}
	if j.Origin == "ru" {
		var rf []string
		goRole := 0
		if j.Go {
			goRole = 2
		}
		for _, f := range j.Flags {
			if strings.HasPrefix(f, "РФ: ") {
				rf = append(rf, strings.TrimPrefix(f, "РФ: "))
			}
			if f == "Go не основной язык" {
				goRole = 1
			}
		}
		m := map[string]any{"title": j.EffTitle(), "company": j.EffCompany(), "url": j.URL,
			"source": rawSource(j.Source), "location": j.Location, "remote": j.Remote, "salary": j.Salary,
			"published_at": j.PostedAt, "description": j.EffDescription(), "rf_limits": rf, "go_role": goRole}
		if ai != nil {
			m["ai"] = ai
		}
		return m
	}
	m := map[string]any{"company": j.EffCompany(), "title": j.EffTitle(), "url": j.URL, "location": j.Location,
		"source": j.Source, "remote": j.Remote, "regions": j.Regions, "go": j.Go, "py": j.Python,
		"years": j.Years, "posted": j.PostedAt, "text": j.EffDescription()}
	for _, f := range j.Flags {
		switch {
		case strings.HasPrefix(f, "ограничение: "):
			m["restrict"] = strings.TrimPrefix(f, "ограничение: ")
		case f == "спонсорство визы":
			m["visa"] = true
		case f == "релокация":
			m["relocation"] = true
		case strings.HasPrefix(f, "текст неполный"):
			m["text_incomplete"] = true
		}
	}
	if ai != nil {
		m["ai"] = ai
	}
	return m
}

// SendResult is what a send did.
type SendResult struct {
	Sent     []int64 `json:"sent"`
	Failed   []int64 `json:"failed"`
	Queued   []int64 `json:"queued"`
	Existing int     `json:"existing"` // found already in JobSync by find_job
	Budget   Budget  `json:"budget"`
	Log      string  `json:"log"`
	Message  string  `json:"message"`
}

// Send sends jobs to JobSync with common/jobsync_push.py. The jobs that do not
// fit in this hour's budget are queued. resend updates jobs already sent
// (upsert by URL, no duplicates).
func Send(ctx context.Context, s *Store, root, python string, ids []int64, checkExisting, resend bool) (SendResult, error) {
	var res SendResult
	b, err := ReadBudget(root)
	if err != nil {
		return res, fmt.Errorf("учёт JobSync не прочитан: %w", err)
	}
	env, _ := EnvStatus(root)
	for _, v := range env {
		if (v.Name == "JOBSYNC_URL" || v.Name == "JOBSYNC_TOKEN") && !v.Set {
			return res, fmt.Errorf("в .env не задан %s — откройте Настройки, там видно, чего не хватает", v.Name)
		}
	}
	var jobs []Job
	for _, id := range ids {
		j, err := s.Get(id)
		if err != nil || j.Status == StatusDismissed {
			continue
		}
		if j.Status == StatusSent && !resend {
			continue
		}
		jobs = append(jobs, j)
	}
	if len(jobs) == 0 {
		res.Budget = b
		res.Message = "нечего отправлять"
		return res, nil
	}
	per := SendCost(1, checkExisting, resend)
	fit := min(len(jobs), b.Remaining/per)
	for _, j := range jobs[fit:] {
		res.Queued = append(res.Queued, j.ID)
	}
	if err := s.SetQueued(res.Queued, true); err != nil {
		return res, err
	}
	jobs = jobs[:fit]
	if len(jobs) == 0 {
		res.Budget = b
		res.Message = fmt.Sprintf("лимит JobSync на этот час исчерпан: %d в очереди, отправлю через ~%d мин.",
			len(res.Queued), b.ResetMin)
		return res, nil
	}

	dir := filepath.Join(root, "state", "panel", "outbox")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return res, err
	}
	items := make([]map[string]any, len(jobs))
	for i, j := range jobs {
		items[i] = outboxItem(j)
	}
	data, _ := json.MarshalIndent(items, "", " ")
	file := filepath.Join(dir, "send-"+time.Now().Format("20060102-150405.000")+".json")
	if err := os.WriteFile(file, data, 0o644); err != nil {
		return res, err
	}
	defer os.Remove(file)
	args := []string{filepath.Join(root, PushScript), "--input", file, "--limit", strconv.Itoa(len(jobs)),
		"--budget", strconv.Itoa(JobSyncBudget)}
	if resend {
		args = append(args, "--resend")
	} else if checkExisting {
		args = append(args, "--check-existing")
	}
	log, code, err := runCommand(ctx, python, root, args, nil)
	res.Log = log
	res.Existing = strings.Count(log, "уже есть в JobSync")
	if err != nil {
		return res, err
	}
	l, lerr := ReadLedger(root)
	if lerr != nil {
		return res, lerr
	}
	for _, j := range jobs {
		at, ok := l.Pushed[j.URL]
		switch {
		case ok && resend:
			s.TouchSent(j.URL)
			res.Sent = append(res.Sent, j.ID)
		case ok:
			s.MarkSent(j.URL, at, false)
			res.Sent = append(res.Sent, j.ID)
		default:
			res.Failed = append(res.Failed, j.ID)
		}
	}
	if strings.Contains(log, "лимит на час почти исчерпан") || strings.Contains(strings.ToLower(log), "rate limit") {
		s.SetQueued(res.Failed, true) // try again when the budget frees up
		res.Queued, res.Failed = append(res.Queued, res.Failed...), nil
	}
	res.Budget, _ = ReadBudget(root)
	res.Message = fmt.Sprintf("отправлено %d", len(res.Sent))
	if res.Existing > 0 {
		res.Message += fmt.Sprintf(" (из них уже были в JobSync: %d)", res.Existing)
	}
	if len(res.Failed) > 0 {
		res.Message += fmt.Sprintf(", не удалось %d — подробности в журнале", len(res.Failed))
	}
	if len(res.Queued) > 0 {
		res.Message += fmt.Sprintf(", в очереди %d", len(res.Queued))
	}
	if code != 0 && len(res.Sent) == 0 {
		res.Message = "jobsync_push.py завершился с ошибкой: " + lastLine(log)
	}
	return res, nil
}

func lastLine(log string) string {
	lines := strings.Split(strings.TrimSpace(log), "\n")
	return lines[len(lines)-1]
}
