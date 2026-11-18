export type Toast = { id: number; text: string; kind: "info" | "error"; action?: { label: string; run: () => void } };

export const toasts = $state<Toast[]>([]);
let next = 1;

export function toast(text: string, opts: { kind?: "info" | "error"; action?: Toast["action"]; ms?: number } = {}) {
  const t: Toast = { id: next++, text, kind: opts.kind ?? "info", action: opts.action };
  toasts.push(t);
  if (toasts.length > 4) toasts.shift();
  setTimeout(() => dismiss(t.id), opts.ms ?? (opts.kind === "error" ? 9000 : 5000));
}

export function dismiss(id: number) {
  const i = toasts.findIndex((t) => t.id === id);
  if (i >= 0) toasts.splice(i, 1);
}
