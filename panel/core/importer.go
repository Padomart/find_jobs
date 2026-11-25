package core

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// The files the scripts write, relative to the project root.
var ImportFiles = []string{"en/out/jobs_noats.json", "en/out/boards.json", "ru/out/jobs_ru.json"}

// LedgerFile is the ledger of common/jobsync_push.py: what was sent and the spent budget.
const LedgerFile = "state/jobsync_ledger.json"

// ---- the scripts' formats (see en/find_boards.py and ru/find_ru_jobs.py) ----

type rawAI struct {
	Score    float64  `json:"score"`
	Level    string   `json:"level"`
	GoStack  bool     `json:"go_stack"`
	Reason   string   `json:"reason"`
	RedFlags []string `json:"red_flags"`
}

func (r *rawAI) toAI() *AI {
	if r == nil {
		return nil
	}
	return &AI{Score: int(r.Score), Level: r.Level, GoStack: r.GoStack, Reason: r.Reason, RedFlags: r.RedFlags}
}

// flexBool reads true/false, 0/1/2 (boards' go is 0, 1 or 2) and null.
type flexBool bool

func (b *flexBool) UnmarshalJSON(d []byte) error {
	s := strings.TrimSpace(string(d))
	*b = flexBool(s != "false" && s != "null" && s != "0" && s != `""`)
	return nil
}

// en/out/jobs_noats.json and the jobs inside en/out/boards.json
type boardsJob struct {
	Company        string   `json:"company"`
	Title          string   `json:"title"`
	URL            string   `json:"url"`
	Location       string   `json:"location"`
	Source         string   `json:"source"`
	Remote         flexBool `json:"remote"`
	Regions        []string `json:"regions"`
	Go             flexBool `json:"go"`
	Py             flexBool `json:"py"`
	Level          string   `json:"level"`
	Years          *int     `json:"years"`
	Relocation     flexBool `json:"relocation"`
	Visa           flexBool `json:"visa"`
	Text           string   `json:"text"`
	Posted         string   `json:"posted"`
	Restrict       string   `json:"restrict"`
	TextIncomplete bool     `json:"text_incomplete"`
	Stale          bool     `json:"stale"`
	Reposts        []any    `json:"reposts"`
	AI             *rawAI   `json:"ai"`
}

type boardsBoard struct {
	Company  string      `json:"company"`
	Provider string      `json:"provider"`
	URL      string      `json:"url"`
	Jobs     []boardsJob `json:"jobs"`
}

// ru/out/jobs_ru.json
type ruJob struct {
	Title       string   `json:"title"`
	Company     string   `json:"company"`
	URL         string   `json:"url"`
	Source      string   `json:"source"`
	Location    string   `json:"location"`
	Remote      flexBool `json:"remote"`
	Salary      string   `json:"salary"`
	PublishedAt string   `json:"published_at"`
	Description string   `json:"description"`
	Big         bool     `json:"big"`
	GoRole      int      `json:"go_role"`
	RFLimits    []string `json:"rf_limits"`
	Score       *float64 `json:"score"`
	AI          *rawAI   `json:"ai"`
}

var sourceNames = map[string]string{
	"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
	"habr": "Хабр Карьера", "superjob": "SuperJob", "trudvsem": "Работа России", "telegram": "Telegram",
	"yandex": "Яндекс", "vk": "VK", "mts": "МТС",
}

func niceSource(s string) string {
	if v, ok := sourceNames[s]; ok {
		return v
	}
	return s
}

func fromBoardsJob(r boardsJob, origin, company, source string) Job {
	j := Job{URL: strings.TrimSpace(r.URL), Origin: origin, Source: niceSource(source), Title: r.Title,
		Company: company, Location: r.Location, Remote: bool(r.Remote), Description: r.Text,
		Regions: r.Regions, Go: bool(r.Go), Python: bool(r.Py), Market: "world", PostedAt: r.Posted}
	if r.Years != nil {
		j.Years = *r.Years
	}
	if r.AI != nil {
		j.AI = r.AI.toAI()
		v := r.AI.Score
		j.Score, j.ScoreKind = &v, "ai"
	}
	if r.Restrict != "" {
		j.Flags = append(j.Flags, "ограничение: "+r.Restrict)
	}
	if bool(r.Visa) {
		j.Flags = append(j.Flags, "спонсорство визы")
	} else if bool(r.Relocation) {
		j.Flags = append(j.Flags, "релокация")
	}
	if r.TextIncomplete {
		j.Flags = append(j.Flags, "текст неполный — проверьте оригинал")
	}
	if r.Stale {
		j.Flags = append(j.Flags, "старое объявление")
	}
	if len(r.Reposts) > 0 {
		j.Flags = append(j.Flags, fmt.Sprintf("репост (%d раз)", len(r.Reposts)))
	}
	if r.Level != "" {
		j.Flags = append(j.Flags, "уровень: "+r.Level)
	}
	return j
}

func fromRuJob(r ruJob) Job {
	j := Job{URL: strings.TrimSpace(r.URL), Origin: "ru", Source: niceSource(r.Source), Title: r.Title,
		Company: r.Company, Location: r.Location, Remote: bool(r.Remote), Salary: r.Salary,
		Description: r.Description, Market: "ru", PostedAt: r.PublishedAt, Go: r.GoRole > 0,
		Python: strings.Contains(strings.ToLower(r.Title+" "+r.Description), "python")}
	if r.AI != nil {
		j.AI = r.AI.toAI()
		v := r.AI.Score
		j.Score, j.ScoreKind = &v, "ai"
	} else if r.Score != nil {
		v := *r.Score
		j.Score, j.ScoreKind = &v, "heuristic"
	}
	for _, l := range r.RFLimits {
		j.Flags = append(j.Flags, "РФ: "+l)
	}
	if r.GoRole == 1 {
		j.Flags = append(j.Flags, "Go не основной язык")
	}
	if r.Big {
		j.Flags = append(j.Flags, "крупная компания")
	}
	return j
}

// readJobs reads one of ImportFiles into jobs, collapsing duplicate URLs.
func readJobs(path string) (jobs []Job, read, dupes int, err error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, 0, 0, err
	}
	var all []Job
	switch filepath.Base(path) {
	case "jobs_noats.json":
		var raw []boardsJob
		if err = json.Unmarshal(data, &raw); err != nil {
			return
		}
		for _, r := range raw {
			all = append(all, fromBoardsJob(r, "boards_noats", r.Company, r.Source))
		}
	case "boards.json":
		var raw []boardsBoard
		if err = json.Unmarshal(data, &raw); err != nil {
			return
		}
		for _, b := range raw {
			for _, r := range b.Jobs {
				j := fromBoardsJob(r, "boards_board", b.Company, b.Provider)
				j.Flags = append(j.Flags, "борд: "+b.URL)
				all = append(all, j)
			}
		}
	case "jobs_ru.json":
		var raw []ruJob
		if err = json.Unmarshal(data, &raw); err != nil {
			return
		}
		for _, r := range raw {
			all = append(all, fromRuJob(r))
		}
	default:
		return nil, 0, 0, fmt.Errorf("неизвестный файл %s", path)
	}
	seen := map[string]int{}
	for _, j := range all {
		if j.URL == "" || j.Title == "" {
			continue
		}
		read++
		if i, ok := seen[j.URL]; ok {
			dupes++
			if len(j.Description) > len(jobs[i].Description) { // keep the fuller copy, and the score of either
				if j.AI == nil && j.Score == nil {
					j.AI, j.Score, j.ScoreKind = jobs[i].AI, jobs[i].Score, jobs[i].ScoreKind
				}
				jobs[i] = j
			} else if jobs[i].AI == nil && jobs[i].Score == nil {
				jobs[i].AI, jobs[i].Score, jobs[i].ScoreKind = j.AI, j.Score, j.ScoreKind
			}
			continue
		}
		seen[j.URL] = len(jobs)
		jobs = append(jobs, j)
	}
	return jobs, read, dupes, nil
}

// ImportFile imports one file. Idempotent: jobs are matched by URL, and a
// second import adds nothing, keeping statuses, notes and edits.
func (s *Store) ImportFile(path string) ImportResult {
	res := ImportResult{File: path}
	jobs, read, dupes, err := readJobs(path)
	res.Read, res.Dupes = read, dupes
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			res.Error = "файла нет — скрипт ещё не запускался"
		} else {
			res.Error = err.Error()
		}
		return res
	}
	tx, err := s.db.Begin()
	if err != nil {
		res.Error = err.Error()
		return res
	}
	defer tx.Rollback()
	for i := range jobs {
		added, err := s.upsert(tx, &jobs[i])
		if err != nil {
			res.Error = err.Error()
			return res
		}
		if added {
			res.Added++
		} else {
			res.Updated++
		}
	}
	if err := tx.Commit(); err != nil {
		res.Error = err.Error()
	}
	return res
}

// Ledger is state/jobsync_ledger.json.
type Ledger struct {
	Units  []float64         `json:"units"`
	Pushed map[string]string `json:"pushed"`
}

// ReadLedger reads the ledger; a missing file is an empty ledger.
func ReadLedger(root string) (Ledger, error) {
	var l Ledger
	data, err := os.ReadFile(filepath.Join(root, LedgerFile))
	if errors.Is(err, os.ErrNotExist) {
		return l, nil
	}
	if err != nil {
		return l, err
	}
	err = json.Unmarshal(data, &l)
	return l, err
}

// SyncLedger marks as sent the jobs the ledger says were sent. Returns how many changed.
func (s *Store) SyncLedger(root string) (int, error) {
	l, err := ReadLedger(root)
	if err != nil {
		return 0, err
	}
	n := 0
	for url, at := range l.Pushed {
		if ok, err := s.MarkSent(url, at, true); err != nil {
			return n, err
		} else if ok {
			n++
		}
	}
	return n, nil
}

// ImportAll imports every file of the scripts and syncs the ledger.
func (s *Store) ImportAll(root string) ([]ImportResult, error) {
	var out []ImportResult
	for _, f := range ImportFiles {
		out = append(out, s.ImportFile(filepath.Join(root, f)))
	}
	_, err := s.SyncLedger(root)
	return out, err
}
