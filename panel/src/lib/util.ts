import { isCallError } from "mygo-runtime";
import type { Job } from "../mygo";

/** Text of an error from Go (already in Russian) or anything else. */
export function errText(e: unknown): string {
  if (isCallError(e)) return e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}

const rtf = new Intl.RelativeTimeFormat("ru", { numeric: "auto" });

/** "2 дня назад", "вчера", "только что". */
export function ago(iso: string | undefined | null): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const s = (t - Date.now()) / 1000;
  const a = Math.abs(s);
  if (a < 60) return "только что";
  if (a < 3600) return rtf.format(Math.round(s / 60), "minute");
  if (a < 86400) return rtf.format(Math.round(s / 3600), "hour");
  if (a < 86400 * 30) return rtf.format(Math.round(s / 86400), "day");
  if (a < 86400 * 365) return rtf.format(Math.round(s / (86400 * 30)), "month");
  return rtf.format(Math.round(s / (86400 * 365)), "year");
}

/** Short age for list rows: "3 дн.", "2 мес.". */
export function shortAge(iso: string): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const d = (Date.now() - t) / 86400000;
  if (d < 1) return "сегодня";
  if (d < 30) return `${Math.floor(d)} дн.`;
  if (d < 365) return `${Math.floor(d / 30)} мес.`;
  return `${Math.floor(d / 365)} г.`;
}

export function fmtDate(iso: string): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  return new Date(t).toLocaleString("ru", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

/** Score colour: red → amber → green, 0..10. */
export function scoreColor(s: number | null): string {
  if (s == null) return "var(--muted-2)";
  if (s >= 7) return "var(--green)";
  if (s >= 5) return "var(--amber)";
  return "var(--red)";
}

export const statusMeta: Record<string, { label: string; icon: string; cls: string }> = {
  new: { label: "Новая", icon: "●", cls: "st-new" },
  shortlisted: { label: "Оставлена", icon: "★", cls: "st-short" },
  sent: { label: "В JobSync", icon: "✓", cls: "st-sent" },
  dismissed: { label: "В корзине", icon: "✕", cls: "st-dism" },
};

export const title = (j: Job) => j.editTitle || j.title;
export const company = (j: Job) => j.editCompany || j.company;
export const description = (j: Job) => j.editDescription || j.description;

export function region(j: Job): string {
  if (j.market === "ru") return j.remote ? "удалёнка" : j.location || "РФ";
  const r = (j.regions || []).filter((x) => x !== "other");
  if (j.remote) return r.length ? `remote · ${r.join(", ")}` : "remote";
  return j.location || r.join(", ");
}

/** Blocks of a plain-text description: paragraphs and lists, no HTML. */
export type Block = { kind: "p"; text: string } | { kind: "ul"; items: string[] };

const bullet = /^\s*(?:[-–—•·▪●◦*✓✔➤►]|\d{1,2}[.)])\s+(.+)$/;

export function blocks(text: string): Block[] {
  const out: Block[] = [];
  for (const raw of (text || "").replace(/\r/g, "").split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const m = bullet.exec(line);
    const last = out[out.length - 1];
    if (m) {
      if (last && last.kind === "ul") last.items.push(m[1]!);
      else out.push({ kind: "ul", items: [m[1]!] });
    } else out.push({ kind: "p", text: line });
  }
  return out;
}
