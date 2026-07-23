// Rule-form ↔ graph translation for flow authoring (Ticket 08).
//
// The operator authors a flow through a linear 5-section form; on save it is SERIALIZED
// into the Ticket-02 graph JSON that the backend validator/interpreter consumes:
//   { origin, default_fallback, nodes: { <id>: { type, next: { <port>: <target-id> }, ...modifiers } } }
//
// Everything the form emits is tagged `origin: "rule-form"` (on the graph and every node)
// so a future graph→form round-trip can recognize form-authored flows. `parseGraph` below
// is that round-trip: it rebuilds the form state from a rule-form graph (best effort; it
// relies on the stable node ids this emitter produces).
//
// `record` is a MODIFIER on the greeting `play` node (never its own node type) — matches
// backend/app/flows/validator.py, which ignores modifiers and only looks at type + next.

export const ORIGIN = "rule-form";

export const MENU_DIGITS = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "#"];

export type MenuOption = {
  digit: string;
  kind: "dial" | "voicemail";
  number: string; // phone number when kind === "dial"
  label: string; // operator-facing description (stored as a modifier)
};

// The full form model — one field group per section, in form order.
export type FlowForm = {
  // (1) Business hours
  hoursEnabled: boolean;
  hoursSchedule: string; // free-text schedule, e.g. "Mon–Fri 9–5" (stored as a modifier)
  // (2) Greeting + record
  greeting: string;
  record: boolean;
  consentNotice: string; // optional consent-notice text (only meaningful when record)
  // (3) IVR menu (optional)
  menuEnabled: boolean;
  menuPrompt: string;
  menuOptions: MenuOption[];
  // (4) Default routing (where the greeting leads when there is no menu)
  defaultRouting: "dial" | "voicemail";
  defaultDialNumber: string;
  // (5) Fallback (flow-level default_fallback)
  fallbackPrompt: string;
};

export function emptyForm(): FlowForm {
  return {
    hoursEnabled: false,
    hoursSchedule: "",
    greeting: "Thanks for calling.",
    record: false,
    consentNotice: "",
    menuEnabled: false,
    menuPrompt: "",
    menuOptions: [],
    defaultRouting: "voicemail",
    defaultDialNumber: "",
    fallbackPrompt: "Sorry we missed you — please leave a message.",
  };
}

// Stable node ids so parseGraph can round-trip a form-authored graph.
const ID = {
  entry: "entry",
  hours: "hours",
  greeting: "greeting",
  menu: "menu",
  routeDial: "route_default",
  voicemail: "voicemail",
  done: "done",
  menuOpt: (digit: string) => `opt_${digit === "*" ? "star" : digit === "#" ? "pound" : digit}`,
};

// Serialize the form into a valid Ticket-02 graph.
export function buildGraph(f: FlowForm): any {
  const nodes: Record<string, any> = {};

  // Flow-level fallback: a voicemail node, always present and always the default_fallback.
  nodes[ID.voicemail] = {
    type: "voicemail",
    origin: ORIGIN,
    ...(f.fallbackPrompt.trim() ? { prompt: f.fallbackPrompt.trim() } : {}),
    next: {},
  };

  // A terminal hangup created lazily — only when a dial node needs an `answered` landing,
  // so we never emit an unreachable node (which would be a validator warning).
  let doneCreated = false;
  const ensureDone = (): string => {
    if (!doneCreated) {
      nodes[ID.done] = { type: "hangup", origin: ORIGIN, next: {} };
      doneCreated = true;
    }
    return ID.done;
  };
  const dialNode = (id: string, number: string, label?: string): string => {
    nodes[id] = {
      type: "dial",
      origin: ORIGIN,
      number,
      ...(label ? { label } : {}),
      // answered → hang up when the leg ends; every failure path falls to voicemail.
      next: { answered: ensureDone(), noanswer: ID.voicemail, busy: ID.voicemail, failed: ID.voicemail },
    };
    return id;
  };

  // (4) Default routing target — reused as the menu's timeout/invalid landing too.
  let routeTarget: string;
  if (f.defaultRouting === "dial" && f.defaultDialNumber.trim()) {
    routeTarget = dialNode(ID.routeDial, f.defaultDialNumber.trim());
  } else {
    routeTarget = ID.voicemail;
  }

  // (3) IVR menu (optional).
  let afterGreeting: string;
  if (f.menuEnabled) {
    const next: Record<string, string> = {};
    for (const opt of f.menuOptions) {
      if (!opt.digit) continue;
      if (opt.kind === "dial" && opt.number.trim()) {
        next[opt.digit] = dialNode(ID.menuOpt(opt.digit), opt.number.trim(), opt.label.trim() || undefined);
      } else {
        next[opt.digit] = ID.voicemail;
      }
    }
    // Unmatched / no-input falls through to the default route.
    next.timeout = routeTarget;
    next.invalid = routeTarget;
    nodes[ID.menu] = {
      type: "menu",
      origin: ORIGIN,
      ...(f.menuPrompt.trim() ? { prompt: f.menuPrompt.trim() } : {}),
      next,
    };
    afterGreeting = ID.menu;
  } else {
    afterGreeting = routeTarget;
  }

  // (2) Greeting play node (+ record modifier).
  const greeting: any = {
    type: "play",
    origin: ORIGIN,
    ...(f.greeting.trim() ? { prompt: f.greeting.trim() } : {}),
    next: { default: afterGreeting },
  };
  if (f.record) {
    greeting.record = true;
    if (f.consentNotice.trim()) greeting.consent_notice = f.consentNotice.trim();
  }
  nodes[ID.greeting] = greeting;

  // (1) Business hours + the single entry node.
  if (f.hoursEnabled) {
    nodes[ID.hours] = {
      type: "hours",
      origin: ORIGIN,
      ...(f.hoursSchedule.trim() ? { schedule: f.hoursSchedule.trim() } : {}),
      next: { open: ID.greeting, closed: ID.voicemail },
    };
    nodes[ID.entry] = { type: "entry", origin: ORIGIN, next: { default: ID.hours } };
  } else {
    nodes[ID.entry] = { type: "entry", origin: ORIGIN, next: { default: ID.greeting } };
  }

  return { origin: ORIGIN, default_fallback: ID.voicemail, nodes };
}

// Best-effort round-trip: rebuild the form from a rule-form graph. Returns null when the
// graph was NOT authored by this form (no origin marker / unknown shape), in which case the
// editor should fall back to a fresh form rather than mis-reconstruct a hand-built graph.
export function parseGraph(graph: any): FlowForm | null {
  if (!graph || graph.origin !== ORIGIN || typeof graph.nodes !== "object") return null;
  const n = graph.nodes as Record<string, any>;
  const f = emptyForm();

  const hours = n[ID.hours];
  f.hoursEnabled = !!hours;
  if (hours) f.hoursSchedule = hours.schedule || "";

  const greeting = n[ID.greeting] || {};
  f.greeting = greeting.prompt || "";
  f.record = !!greeting.record;
  f.consentNotice = greeting.consent_notice || "";

  const menu = n[ID.menu];
  f.menuEnabled = !!menu;
  if (menu && menu.next) {
    f.menuPrompt = menu.prompt || "";
    f.menuOptions = MENU_DIGITS.filter((d) => d in menu.next).map((digit) => {
      const target = n[menu.next[digit]];
      if (target && target.type === "dial") {
        return { digit, kind: "dial", number: target.number || "", label: target.label || "" };
      }
      return { digit, kind: "voicemail", number: "", label: "" };
    });
  }

  const routeDial = n[ID.routeDial];
  if (routeDial && routeDial.type === "dial") {
    f.defaultRouting = "dial";
    f.defaultDialNumber = routeDial.number || "";
  } else {
    f.defaultRouting = "voicemail";
  }

  const vm = n[ID.voicemail] || {};
  f.fallbackPrompt = vm.prompt || "";

  return f;
}
