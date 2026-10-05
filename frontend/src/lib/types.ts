/** Types mirroring the backend payloads (Steps 6-9). */

export type Role = "nurse" | "doctor" | "admin";
export type RiskLevel = "low" | "medium" | "high" | "unknown";

export interface StaffUser {
  username: string;
  display_name: string;
  role: Role;
  hospital: string;
  /** 4 Oct 2026: where this person's HIGH-risk alert is emailed. */
  email: string;
  active: boolean;
  last_login_at: string | null;
}

/** One person a HIGH-risk call alerted, and how their delivery went. */
export interface AlertRecipient {
  username: string;
  display_name: string;
  role: string;
  email: string;
  /** sent | failed: <why> | no_email | not_configured | disabled */
  status: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: StaffUser;
}

export interface MeResponse {
  via: string;
  role: string;
  username: string;
  display_name: string;
  user: StaffUser | null;
}

export type RolePermissions = Record<string, Role[]>;

export interface RolesPayload {
  roles: Role[];
  permissions: RolePermissions;
}

export interface AnswerSummary {
  question_id: string;
  kind: string;
  interpretation: boolean | string | null;
  transcript: string;
}

export interface Finding {
  id: string;
  label: string;
  severity: string;
  red_flag: boolean;
  points: number;
  matched_text: string;
  source: string;
}

/** Compact row from /dashboard/* (no transcript body). */
export interface CallRow {
  id: number;
  provider_call_id: string;
  patient_code: string;
  phone_number: string;
  diagnosis_category: string;
  started_at: string | null;
  finished_at?: string | null;
  duration_sec: number;
  ended_reason: string;
  risk_level: RiskLevel;
  risk_score: number;
  risk_reasons?: string[];
  findings?: Finding[];
  answers?: AnswerSummary[];
  alert_status: string;
  alert_detail?: string;
  /** Step 7: the prepared alert text (always stored for a HIGH-risk call). */
  alert_message?: string;
  /**
   * 4 Oct 2026: who the HIGH-risk call emailed, and each person's status.
   * Lets the board answer "did the right people actually hear?".
   */
  alert_recipients?: AlertRecipient[];
  reviewed: boolean;
  nurse_note?: string;
  closed_by?: string;
  closed_at?: string | null;
}

export interface Patient {
  id: number;
  patient_code: string;
  name: string;
  phone_number: string;
  diagnosis_category: string;
  discharge_date: string;
  notes: string;
  language_pref: string;
  active: boolean;
  created_at: string | null;
}

export interface ScheduleSlot {
  date: string;
  due_at: string;
  status: string;
  days_overdue: number;
}

export interface ScheduleRow {
  patient_code: string;
  name: string;
  phone_number: string;
  diagnosis_category: string;
  discharge_date: string;
  offsets_days: number[];
  slots: ScheduleSlot[];
  next_call_at: string | null;
  last_call_at: string | null;
  status: string;
  days_overdue: number;
  scheduler_enabled: boolean;
  will_dial_automatically: boolean;
  /** 0 = auto-diallable now; >0 = hours until the next automatic dial. */
  cooldown_hours?: number;
}

export interface SchedulerStatus {
  enabled: boolean;
  running: boolean;
  dry_run: boolean;
  interval_minutes: number;
  checkin_offsets_days: number[];
  slot_local_time: string;
  grace_days: number;
  max_dials_per_tick: number;
  /** Hours before the same patient may be auto-dialed again (default 24). */
  min_hours_between_calls: number;
  next_tick_at: string | null;
  note?: string;
  /** Present on POST /schedule/enabled: human summary of the flip. */
  message?: string;
}

export interface ScheduleBoard {
  scheduler: SchedulerStatus;
  count: number;
  due_count: number;
  patients: ScheduleRow[];
}

export interface DashboardSummary {
  generated_at: string;
  cards: {
    calls_total: number;
    calls_by_risk: Record<string, number>;
    calls_reviewed: number;
    alerts_sent: number;
    /** HIGH-risk calls whose alert text is prepared and waiting (Step 7). */
    alerts_ready: number;
    alerts_open: number;
    patients_total: number;
    patients_active: number;
    calls_due: number;
    calls_due_total: number;
    last_call_at: string | null;
  };
  recent_calls: CallRow[];
  open_alerts: CallRow[];
  due_patients: {
    patient_code: string;
    name: string;
    phone_number: string;
    next_call_at: string | null;
    status: string;
    days_overdue: number;
  }[];
  scheduler: SchedulerStatus;
  alerts: {
    enabled: boolean;
    /** "ready" (prepared in-dashboard) or "whatsapp" (Zernio sandbox). */
    delivery: string;
    channel: string;
    conversation_configured: boolean;
    /** 4 Oct 2026: the second channel -- named staff, routed on risk score. */
    email_enabled: boolean;
    email_configured: boolean;
    email_sender: string;
    /** Above this score doctors are emailed too (nurses always are). */
    doctor_score_threshold: number;
  };
  cost_rails: {
    max_call_duration_sec: number;
    max_calls_per_hour: number;
    max_calls_per_day: number;
    max_concurrent_calls: number;
    live_streams: number;
  };
}

/** POST /calls response -- means "Zernio started dialing", not "answered". */
export interface DialResponse {
  status: string;
  to: string;
  provider_call_id: string;
  provider_status: string;
  stream: string;
  greeting: string;
  diagnosis_category: string;
}

/** POST /schedule/run-now response. */
export interface RunNowResponse {
  ran_at: string;
  enabled: boolean;
  dry_run: boolean;
  planned_count: number;
  dialed: number;
  skipped: number;
  planned: Array<Record<string, unknown>>;
  outcomes: Array<Record<string, unknown>>;
  message: string;
}