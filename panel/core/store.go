package core

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "modernc.org/sqlite" // pure Go SQLite, no cgo
)

// Store is the SQLite database of the panel (state/panel/panel.db).
type Store struct {
	db *sql.DB
}

const schema = `
CREATE TABLE IF NOT EXISTS jobs (
	id INTEGER PRIMARY KEY,
	url TEXT NOT NULL UNIQUE,
	origin TEXT NOT NULL DEFAULT '',
	source TEXT NOT NULL DEFAULT '',
	title TEXT NOT NULL DEFAULT '',
	company TEXT NOT NULL DEFAULT '',
	location TEXT NOT NULL DEFAULT '',
	remote INTEGER NOT NULL DEFAULT 0,
	salary TEXT NOT NULL DEFAULT '',
	description TEXT NOT NULL DEFAULT '',
	score REAL,
	score_kind TEXT NOT NULL DEFAULT '',
	ai TEXT NOT NULL DEFAULT '',
	regions TEXT NOT NULL DEFAULT '[]',
	years INTEGER NOT NULL DEFAULT 0,
	go INTEGER NOT NULL DEFAULT 0,
	python INTEGER NOT NULL DEFAULT 0,
	market TEXT NOT NULL DEFAULT 'world',
	flags TEXT NOT NULL DEFAULT '[]',
	posted_at TEXT NOT NULL DEFAULT '',
	status TEXT NOT NULL DEFAULT 'new',
	notes TEXT NOT NULL DEFAULT '',
	edit_title TEXT NOT NULL DEFAULT '',
	edit_company TEXT NOT NULL DEFAULT '',
	edit_description TEXT NOT NULL DEFAULT '',
	queued INTEGER NOT NULL DEFAULT 0,
	first_seen TEXT NOT NULL,
	updated_at TEXT NOT NULL,
	sent_at TEXT NOT NULL DEFAULT '',
	status_changed_at TEXT NOT NULL DEFAULT '',
	search TEXT NOT NULL DEFAULT '' -- lowercased in Go: SQLite's lower() knows ASCII only
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS runs (
	id INTEGER PRIMARY KEY,
	script TEXT NOT NULL,
	args TEXT NOT NULL DEFAULT '',
	started TEXT NOT NULL,
	finished TEXT NOT NULL DEFAULT '',
	exit_code INTEGER,
	imported INTEGER NOT NULL DEFAULT 0,
	updated INTEGER NOT NULL DEFAULT 0,
	status TEXT NOT NULL DEFAULT 'running',
	log TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
`

// OpenStore opens (and creates) the database at path.
func OpenStore(path string) (*Store, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", "file:"+path+"?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(1)")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1) // one writer: no SQLITE_BUSY between our own goroutines
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("схема базы: %w", err)
	}
	// a run that was going when the app quit did not finish
	db.Exec(`UPDATE runs SET status='canceled', finished=? WHERE status='running'`, now())
	return &Store{db: db}, nil
}

func (s *Store) Close() error { return s.db.Close() }

func now() string { return time.Now().UTC().Format(time.RFC3339) }

// ---------- jobs ----------

const jobCols = `id,url,origin,source,title,company,location,remote,salary,description,score,score_kind,ai,regions,
years,go,python,market,flags,posted_at,status,notes,edit_title,edit_company,edit_description,queued,first_seen,
updated_at,sent_at,status_changed_at`

func scanJob(sc interface{ Scan(...any) error }) (Job, error) {
	var j Job
	var score sql.NullFloat64
	var ai, regions, flags, status string
	err := sc.Scan(&j.ID, &j.URL, &j.Origin, &j.Source, &j.Title, &j.Company, &j.Location, &j.Remote, &j.Salary,
		&j.Description, &score, &j.ScoreKind, &ai, &regions, &j.Years, &j.Go, &j.Python, &j.Market, &flags,
		&j.PostedAt, &status, &j.Notes, &j.EditTitle, &j.EditCompany, &j.EditDesc, &j.Queued, &j.FirstSeen,
		&j.UpdatedAt, &j.SentAt, &j.StatusChanged)
	if err != nil {
		return j, err
	}
	j.Status = Status(status)
	if score.Valid {
		v := score.Float64
		j.Score = &v
	}
	if ai != "" {
		j.AI = &AI{}
		_ = json.Unmarshal([]byte(ai), j.AI)
	}
	_ = json.Unmarshal([]byte(regions), &j.Regions)
	_ = json.Unmarshal([]byte(flags), &j.Flags)
	if j.Regions == nil {
		j.Regions = []string{}
	}
	if j.Flags == nil {
		j.Flags = []string{}
	}
	return j, nil
}

func js(v any) string {
	b, _ := json.Marshal(v)
	if string(b) == "null" {
		return "[]"
	}
	return string(b)
}

// upsert inserts a job or refreshes its original fields. It never touches the
// status, notes or the user's edits. It reports whether the job is new.
func (s *Store) upsert(tx *sql.Tx, j *Job) (added bool, err error) {
	ai := ""
	if j.AI != nil {
		ai = js(j.AI)
	}
	var score any
	if j.Score != nil {
		score = *j.Score
	}
	t := now()
	res, err := tx.Exec(`INSERT INTO jobs (url,origin,source,title,company,location,remote,salary,description,score,
		score_kind,ai,regions,years,go,python,market,flags,posted_at,status,first_seen,updated_at,status_changed_at)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
		ON CONFLICT(url) DO NOTHING`,
		j.URL, j.Origin, j.Source, j.Title, j.Company, j.Location, j.Remote, j.Salary, j.Description, score,
		j.ScoreKind, ai, js(j.Regions), j.Years, j.Go, j.Python, j.Market, js(j.Flags), j.PostedAt,
		string(StatusNew), t, t, t)
	if err != nil {
		return false, err
	}
	if n, _ := res.RowsAffected(); n == 1 {
		return true, refreshSearch(tx, j.URL)
	}
	// existing: refresh what the script knows, keep a longer description and an existing score
	_, err = tx.Exec(`UPDATE jobs SET origin=?, source=?, title=?, company=?, location=?, remote=?, salary=?,
		description=CASE WHEN length(?) >= length(description) THEN ? ELSE description END,
		score=COALESCE(?, score), score_kind=CASE WHEN ? IS NULL THEN score_kind ELSE ? END,
		ai=CASE WHEN ?='' THEN ai ELSE ? END, regions=?, years=?, go=?, python=?, market=?, flags=?,
		posted_at=CASE WHEN ?='' THEN posted_at ELSE ? END, updated_at=?
		WHERE url=?`,
		j.Origin, j.Source, j.Title, j.Company, j.Location, j.Remote, j.Salary,
		j.Description, j.Description, score, score, j.ScoreKind, ai, ai, js(j.Regions), j.Years, j.Go, j.Python,
		j.Market, js(j.Flags), j.PostedAt, j.PostedAt, t, j.URL)
	if err != nil {
		return false, err
	}
	return false, refreshSearch(tx, j.URL)
}

type execQuerier interface {
	Exec(string, ...any) (sql.Result, error)
	QueryRow(string, ...any) *sql.Row
}

// refreshSearch rebuilds the lowercased search text of a job (originals, edits, notes).
func refreshSearch(db execQuerier, url string) error {
	var parts [11]string
	err := db.QueryRow(`SELECT title,company,edit_title,edit_company,location,source,notes,description,edit_description,
		salary,flags FROM jobs WHERE url=?`, url).Scan(&parts[0], &parts[1], &parts[2], &parts[3], &parts[4], &parts[5],
		&parts[6], &parts[7], &parts[8], &parts[9], &parts[10])
	if err != nil {
		return err
	}
	text := strings.ReplaceAll(strings.ToLower(strings.Join(parts[:], " ")), "ё", "е")
	_, err = db.Exec(`UPDATE jobs SET search=? WHERE url=?`, text, url)
	return err
}

// Get returns one job.
func (s *Store) Get(id int64) (Job, error) {
	j, err := scanJob(s.db.QueryRow(`SELECT `+jobCols+` FROM jobs WHERE id=?`, id))
	if errors.Is(err, sql.ErrNoRows) {
		return j, fmt.Errorf("вакансия %d не найдена", id)
	}
	return j, err
}

// GetByURL returns one job by its URL.
func (s *Store) GetByURL(url string) (Job, error) {
	return scanJob(s.db.QueryRow(`SELECT `+jobCols+` FROM jobs WHERE url=?`, url))
}

// List returns a page of the jobs the filter selects.
func (s *Store) List(f Filter) (Page, error) {
	where, args := []string{"1=1"}, []any{}
	switch f.Status {
	case "":
		where = append(where, "status != 'dismissed'")
	case "all":
	case "queued":
		where = append(where, "queued=1")
	default:
		where = append(where, "status = ?")
		args = append(args, f.Status)
	}
	if f.Source != "" {
		where = append(where, "source = ?")
		args = append(args, f.Source)
	}
	if f.Market != "" {
		where = append(where, "market = ?")
		args = append(args, f.Market)
	}
	if f.Remote {
		where = append(where, "remote = 1")
	}
	switch f.Lang {
	case "go":
		where = append(where, "go = 1")
	case "python":
		where = append(where, "python = 1")
	}
	if f.MinScore > 0 {
		where = append(where, "score >= ?")
		args = append(args, f.MinScore)
	}
	if q := strings.TrimSpace(f.Query); q != "" {
		for _, w := range strings.Fields(strings.ToLower(q)) {
			where = append(where, `search LIKE ?`)
			args = append(args, "%"+strings.ReplaceAll(w, "ё", "е")+"%")
		}
	}
	order := map[string]string{
		"score":   "score IS NULL, score DESC, posted_at DESC",
		"date":    "posted_at = '', posted_at DESC, id DESC",
		"company": "lower(CASE WHEN edit_company!='' THEN edit_company ELSE company END), id",
		"new":     "first_seen DESC, id DESC",
	}[f.Sort]
	if order == "" {
		order = "score IS NULL, score DESC, posted_at DESC"
	}
	w := strings.Join(where, " AND ")
	var p Page
	if err := s.db.QueryRow(`SELECT count(*) FROM jobs WHERE `+w, args...).Scan(&p.Total); err != nil {
		return p, err
	}
	limit := f.Limit
	if limit <= 0 || limit > 1000 {
		limit = 200
	}
	rows, err := s.db.Query(`SELECT `+jobCols+` FROM jobs WHERE `+w+` ORDER BY `+order+` LIMIT ? OFFSET ?`,
		append(args, limit, f.Offset)...)
	if err != nil {
		return p, err
	}
	defer rows.Close()
	p.Jobs = []Job{}
	for rows.Next() {
		j, err := scanJob(rows)
		if err != nil {
			return p, err
		}
		p.Jobs = append(p.Jobs, j)
	}
	return p, rows.Err()
}

// Sources lists the sources present, for the filter.
func (s *Store) Sources() ([]string, error) {
	rows, err := s.db.Query(`SELECT DISTINCT source FROM jobs WHERE source != '' ORDER BY lower(source)`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []string{}
	for rows.Next() {
		var v string
		rows.Scan(&v)
		out = append(out, v)
	}
	return out, rows.Err()
}

// Counts returns the number of jobs per status.
func (s *Store) Counts() (Counts, error) {
	var c Counts
	rows, err := s.db.Query(`SELECT status, count(*) FROM jobs GROUP BY status`)
	if err != nil {
		return c, err
	}
	defer rows.Close()
	for rows.Next() {
		var st string
		var n int
		rows.Scan(&st, &n)
		switch Status(st) {
		case StatusNew:
			c.New = n
		case StatusShortlisted:
			c.Shortlisted = n
		case StatusSent:
			c.Sent = n
		case StatusDismissed:
			c.Dismissed = n
		}
	}
	s.db.QueryRow(`SELECT count(*) FROM jobs WHERE queued=1`).Scan(&c.Queued)
	return c, rows.Err()
}

// Prior is a job's status before a change, for undo.
type Prior struct {
	ID     int64  `json:"id"`
	Status Status `json:"status"`
	Queued bool   `json:"queued"`
}

// SetStatus moves jobs to a status and returns what they were, for undo.
// Sent is set only by sending; this refuses it.
func (s *Store) SetStatus(ids []int64, st Status) ([]Prior, error) {
	if st == StatusSent {
		return nil, errors.New("статус «отправлено» ставится только отправкой")
	}
	tx, err := s.db.Begin()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	var prior []Prior
	for _, id := range ids {
		var p Prior
		p.ID = id
		var cur, sentAt string
		if err := tx.QueryRow(`SELECT status, queued, sent_at FROM jobs WHERE id=?`, id).Scan(&cur, &p.Queued, &sentAt); err != nil {
			continue
		}
		p.Status = Status(cur)
		prior = append(prior, p)
		target := st
		if sentAt != "" && st == StatusNew {
			target = StatusSent // "вернуть" из корзины вакансию, которая уже в JobSync
		}
		if _, err := tx.Exec(`UPDATE jobs SET status=?, queued=CASE WHEN ?='dismissed' THEN 0 ELSE queued END,
			status_changed_at=?, updated_at=? WHERE id=?`, string(target), string(st), now(), now(), id); err != nil {
			return nil, err
		}
	}
	return prior, tx.Commit()
}

// Restore puts jobs back to what Prior recorded (undo).
func (s *Store) Restore(prior []Prior) error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, p := range prior {
		if _, err := tx.Exec(`UPDATE jobs SET status=?, queued=?, status_changed_at=?, updated_at=? WHERE id=?`,
			string(p.Status), p.Queued, now(), now(), p.ID); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// Edit is the user's changes to a job; empty strings clear an edit.
type Edit struct {
	Title       string `json:"title"`
	Company     string `json:"company"`
	Description string `json:"description"`
	Notes       string `json:"notes"`
}

// SaveEdit stores the user's edits. A field equal to the original is not kept as an edit.
func (s *Store) SaveEdit(id int64, e Edit) (Job, error) {
	j, err := s.Get(id)
	if err != nil {
		return j, err
	}
	norm := func(v, orig string) string {
		v = strings.TrimRight(v, " \n")
		if v == strings.TrimRight(orig, " \n") {
			return ""
		}
		return v
	}
	_, err = s.db.Exec(`UPDATE jobs SET edit_title=?, edit_company=?, edit_description=?, notes=?, updated_at=? WHERE id=?`,
		norm(e.Title, j.Title), norm(e.Company, j.Company), norm(e.Description, j.Description), e.Notes, now(), id)
	if err != nil {
		return j, err
	}
	if err := refreshSearch(s.db, j.URL); err != nil {
		return j, err
	}
	return s.Get(id)
}

// SetQueued marks jobs as waiting for JobSync budget.
func (s *Store) SetQueued(ids []int64, q bool) error {
	for _, id := range ids {
		if _, err := s.db.Exec(`UPDATE jobs SET queued=?, updated_at=? WHERE id=?`, q, now(), id); err != nil {
			return err
		}
	}
	return nil
}

// QueuedIDs lists the jobs waiting to be sent, oldest change first.
func (s *Store) QueuedIDs() ([]int64, error) {
	rows, err := s.db.Query(`SELECT id FROM jobs WHERE queued=1 AND status!='dismissed' ORDER BY updated_at, id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []int64
	for rows.Next() {
		var id int64
		rows.Scan(&id)
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// MarkSent records that JobSync has the job (from the ledger of jobsync_push.py).
// fromLedger=true leaves jobs in the trash alone: the user dismissed them after sending.
func (s *Store) MarkSent(url, at string, fromLedger bool) (bool, error) {
	if at == "" {
		at = now()
	}
	res, err := s.db.Exec(`UPDATE jobs SET status='sent', queued=0, sent_at=CASE WHEN sent_at='' THEN ? ELSE sent_at END,
		status_changed_at=CASE WHEN status!='sent' THEN ? ELSE status_changed_at END, updated_at=?
		WHERE url=? AND (status!='sent' OR queued=1) AND NOT (? AND status='dismissed')`, at, now(), now(), url, fromLedger)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

// TouchSent updates sent_at after a resend.
func (s *Store) TouchSent(url string) error {
	_, err := s.db.Exec(`UPDATE jobs SET sent_at=?, queued=0, updated_at=? WHERE url=?`, now(), now(), url)
	return err
}

// EmptyTrash deletes the dismissed jobs for good and returns how many.
// A later import brings a deleted job back as new only if the script finds it again.
func (s *Store) EmptyTrash() (int, error) {
	res, err := s.db.Exec(`DELETE FROM jobs WHERE status='dismissed'`)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return int(n), nil
}

// ---------- runs ----------

// StartRun records a run that started.
func (s *Store) StartRun(script, args string) (int64, error) {
	res, err := s.db.Exec(`INSERT INTO runs (script,args,started) VALUES (?,?,?)`, script, args, now())
	if err != nil {
		return 0, err
	}
	return res.LastInsertId()
}

// FinishRun records how a run ended.
func (s *Store) FinishRun(id int64, status string, code *int, log string, imported, updated int) error {
	var c any
	if code != nil {
		c = *code
	}
	_, err := s.db.Exec(`UPDATE runs SET finished=?, status=?, exit_code=?, log=?, imported=?, updated=? WHERE id=?`,
		now(), status, c, log, imported, updated, id)
	return err
}

// Runs lists the latest runs, without their logs.
func (s *Store) Runs(limit int) ([]Run, error) {
	rows, err := s.db.Query(`SELECT id,script,args,started,finished,exit_code,imported,updated,status FROM runs
		ORDER BY id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Run{}
	for rows.Next() {
		var r Run
		var code sql.NullInt64
		rows.Scan(&r.ID, &r.Script, &r.Args, &r.Started, &r.Finished, &code, &r.Imported, &r.Updated, &r.Status)
		if code.Valid {
			c := int(code.Int64)
			r.ExitCode = &c
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// RunLog returns one run with its log.
func (s *Store) RunLog(id int64) (Run, error) {
	var r Run
	var code sql.NullInt64
	err := s.db.QueryRow(`SELECT id,script,args,started,finished,exit_code,imported,updated,status,log FROM runs WHERE id=?`, id).
		Scan(&r.ID, &r.Script, &r.Args, &r.Started, &r.Finished, &code, &r.Imported, &r.Updated, &r.Status, &r.Log)
	if code.Valid {
		c := int(code.Int64)
		r.ExitCode = &c
	}
	return r, err
}

// LastRunLog is the log of the latest finished run of a script.
func (s *Store) LastRunLog(script string) (Run, error) {
	var id int64
	err := s.db.QueryRow(`SELECT id FROM runs WHERE script=? AND status!='running' ORDER BY id DESC LIMIT 1`, script).Scan(&id)
	if err != nil {
		return Run{}, err
	}
	return s.RunLog(id)
}

// ---------- settings ----------

func (s *Store) Setting(key, def string) string {
	var v string
	if err := s.db.QueryRow(`SELECT value FROM settings WHERE key=?`, key).Scan(&v); err != nil {
		return def
	}
	return v
}

func (s *Store) SetSetting(key, value string) error {
	_, err := s.db.Exec(`INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value`, key, value)
	return err
}
