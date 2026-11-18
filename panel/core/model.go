// Package core is the panel's engine: the SQLite store, importing the scripts'
// JSON, sending to JobSync through common/jobsync_push.py, running the search
// scripts. It knows nothing about the window shell (mygo or a local site).
package core

// Status of a job in the review queue.
type Status string

const (
	StatusNew         Status = "new"
	StatusShortlisted Status = "shortlisted"
	StatusSent        Status = "sent"
	StatusDismissed   Status = "dismissed" // the trash: restorable until the trash is emptied
)

// AI is the scoring of common/ai_score.py, when the search ran with --ai.
type AI struct {
	Score    int      `json:"score"`
	Level    string   `json:"level,omitempty"`
	GoStack  bool     `json:"goStack"`
	Reason   string   `json:"reason,omitempty"`
	RedFlags []string `json:"redFlags,omitempty"`
}

// Job is one vacancy. The original fields come from the scripts and are
// refreshed on every import; the user's edits live in Edit* fields and win.
type Job struct {
	ID          int64    `json:"id"`
	URL         string   `json:"url"`
	Origin      string   `json:"origin"` // boards_noats, boards_board, ru
	Source      string   `json:"source"`
	Title       string   `json:"title"`
	Company     string   `json:"company"`
	Location    string   `json:"location"`
	Remote      bool     `json:"remote"`
	Salary      string   `json:"salary"`
	Description string   `json:"description"`
	Score       *float64 `json:"score"` // 0-10: AI score, else the script's heuristic
	ScoreKind   string   `json:"scoreKind"`
	AI          *AI      `json:"ai"`
	Regions     []string `json:"regions"`
	Years       int      `json:"years"`
	Go          bool     `json:"go"`
	Python      bool     `json:"python"`
	Market      string   `json:"market"` // ru or world
	Flags       []string `json:"flags"`  // restrictions and warnings to show
	PostedAt    string   `json:"postedAt"`

	Status        Status `json:"status"`
	Notes         string `json:"notes"`
	EditTitle     string `json:"editTitle"`
	EditCompany   string `json:"editCompany"`
	EditDesc      string `json:"editDescription"`
	Queued        bool   `json:"queued"` // waiting for JobSync budget
	FirstSeen     string `json:"firstSeen"`
	UpdatedAt     string `json:"updatedAt"`
	SentAt        string `json:"sentAt"`
	StatusChanged string `json:"statusChangedAt"`
}

// EffTitle, EffCompany and EffDescription are what is shown and sent: the
// user's edit when there is one, else the original.
func (j *Job) EffTitle() string       { return pick(j.EditTitle, j.Title) }
func (j *Job) EffCompany() string     { return pick(j.EditCompany, j.Company) }
func (j *Job) EffDescription() string { return pick(j.EditDesc, j.Description) }

func pick(edit, orig string) string {
	if edit != "" {
		return edit
	}
	return orig
}

// Filter selects jobs for the inbox list.
type Filter struct {
	Status   string  `json:"status"` // "" = everything but the trash; "all"; or a Status
	Source   string  `json:"source"`
	Market   string  `json:"market"` // "", ru, world
	Remote   bool    `json:"remote"`
	Lang     string  `json:"lang"` // "", go, python
	MinScore float64 `json:"minScore"`
	Query    string  `json:"query"`
	Sort     string  `json:"sort"` // score, date, company, new
	Offset   int     `json:"offset"`
	Limit    int     `json:"limit"`
}

// Page is a slice of the filtered list and the total it comes from.
type Page struct {
	Jobs  []Job `json:"jobs"`
	Total int   `json:"total"`
}

// Counts are the jobs per status.
type Counts struct {
	New         int `json:"new"`
	Shortlisted int `json:"shortlisted"`
	Sent        int `json:"sent"`
	Dismissed   int `json:"dismissed"`
	Queued      int `json:"queued"`
}

// Run is one run of a search script.
type Run struct {
	ID       int64  `json:"id"`
	Script   string `json:"script"` // boards or ru
	Args     string `json:"args"`
	Started  string `json:"started"`
	Finished string `json:"finished"`
	ExitCode *int   `json:"exitCode"`
	Imported int    `json:"imported"` // new jobs added to the inbox
	Updated  int    `json:"updated"`
	Log      string `json:"log,omitempty"`
	Status   string `json:"status"` // running, ok, failed, canceled
}

// ImportResult says what one file brought in.
type ImportResult struct {
	File    string `json:"file"`
	Read    int    `json:"read"`
	Added   int    `json:"added"`
	Updated int    `json:"updated"`
	Dupes   int    `json:"dupes"` // collapsed inside the file
	Error   string `json:"error,omitempty"`
}

// SourceHealth is what the last run of a script said about one source.
type SourceHealth struct {
	Script string `json:"script"`
	Source string `json:"source"`
	Got    int    `json:"got"`
	Passed int    `json:"passed"`
	OK     bool   `json:"ok"`
	Note   string `json:"note"`
}

// Pick returns edit when it is set, else orig.
func Pick(edit, orig string) string { return pick(edit, orig) }
