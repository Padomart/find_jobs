// panelctl drives the panel's core from the terminal, without the window:
// import, list, act, edit and send. It shows that core does not depend on mygo.
//
//	go run ./cmd/panelctl -root .. list -status new
//	go run ./cmd/panelctl -root .. send -url https://...
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"panel/core"
)

func main() {
	root := flag.String("root", "..", "папка проекта find_jobs")
	flag.Usage = func() {
		fmt.Fprintln(os.Stderr, "использование: panelctl [-root DIR] import | list [-status S] [-q TEXT] | "+
			"act -url URL -status shortlisted|dismissed|new | edit -url URL [-title T] [-company C] [-desc D] | "+
			"send -url URL [-resend] | run boards|ru | budget | show -url URL")
		flag.PrintDefaults()
	}
	flag.Parse()
	r, _ := filepath.Abs(*root)
	a, err := core.NewApp(r)
	if err != nil {
		fail(err)
	}
	defer a.Close()
	args := flag.Args()
	if len(args) == 0 {
		flag.Usage()
		os.Exit(2)
	}
	fs := flag.NewFlagSet(args[0], flag.ExitOnError)
	url := fs.String("url", "", "ссылка вакансии")
	status := fs.String("status", "", "статус")
	q := fs.String("q", "", "поиск")
	title := fs.String("title", "", "новое название")
	company := fs.String("company", "", "новая компания")
	desc := fs.String("desc", "", "новое описание")
	resend := fs.Bool("resend", false, "обновить уже отправленную")
	fs.Parse(args[1:])

	byURL := func() core.Job {
		j, err := a.Store.GetByURL(*url)
		if err != nil {
			fail(fmt.Errorf("вакансия %s не найдена: %w", *url, err))
		}
		return j
	}
	switch args[0] {
	case "import":
		res, err := a.Import()
		for _, x := range res {
			fmt.Printf("%-28s прочитано %d, новых %d, обновлено %d, дублей %d %s\n", filepath.Base(x.File), x.Read,
				x.Added, x.Updated, x.Dupes, x.Error)
		}
		check(err)
	case "list":
		p, err := a.Store.List(core.Filter{Status: *status, Query: *q, Limit: 1000})
		check(err)
		for _, j := range p.Jobs {
			sc := "  -"
			if j.Score != nil {
				sc = fmt.Sprintf("%4.1f", *j.Score)
			}
			fmt.Printf("%4d %s %-11s %-14s %s — %s  %s\n", j.ID, sc, j.Status, j.Source, j.EffCompany(), j.EffTitle(), j.URL)
		}
		fmt.Println("всего:", p.Total)
	case "show":
		j := byURL()
		fmt.Printf("%s — %s\nстатус %s, отправлено %q, правки: title=%q company=%q desc=%d симв.\n", j.EffCompany(),
			j.EffTitle(), j.Status, j.SentAt, j.EditTitle, j.EditCompany, len(j.EditDesc))
	case "act":
		n, err := a.Act([]int64{byURL().ID}, core.Status(*status))
		check(err)
		fmt.Println("изменено:", n)
	case "edit":
		j := byURL()
		e := core.Edit{Title: core.Pick(*title, j.EffTitle()), Company: core.Pick(*company, j.EffCompany()),
			Description: core.Pick(*desc, j.EffDescription()), Notes: j.Notes}
		j, err := a.Store.SaveEdit(j.ID, e)
		check(err)
		fmt.Printf("сохранено: %s — %s\n", j.EffCompany(), j.EffTitle())
	case "send":
		r, err := a.Send(context.Background(), []int64{byURL().ID}, *resend)
		check(err)
		fmt.Println(strings.TrimSpace(r.Log))
		fmt.Printf("\nитог: %s; отправлено %v, не удалось %v, в очереди %v; лимит: осталось %d из %d\n",
			r.Message, r.Sent, r.Failed, r.Queued, r.Budget.Remaining, r.Budget.Budget)
	case "run":
		script := fs.Arg(0)
		r, err := a.RunScript(context.Background(), script, func(_ int64, t string) { fmt.Println(t) })
		check(err)
		fmt.Printf("\nзапуск #%d: %s, код %v, импортировано новых %d, обновлено %d\n", r.ID, r.Status, *r.ExitCode, r.Imported, r.Updated)
		for _, h := range core.Health(script, r.Log) {
			fmt.Printf("  %-18s ok=%v получено %d прошло %d  %s\n", h.Source, h.OK, h.Got, h.Passed, h.Note)
		}
	case "budget":
		b, err := core.ReadBudget(a.Root)
		check(err)
		fmt.Printf("использовано за час %d, осталось %d из %d (жёсткий лимит JobSync %d)\n", b.Used, b.Remaining, b.Budget, b.HardLimit)
	default:
		flag.Usage()
		os.Exit(2)
	}
}

func check(err error) {
	if err != nil {
		fail(err)
	}
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "ошибка:", err)
	os.Exit(1)
}
