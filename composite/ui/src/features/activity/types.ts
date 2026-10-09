export type Outcome = "success" | "denied" | "failure";

export const OUTCOMES: Outcome[] = ["success", "denied", "failure"];

export interface ActivityItem {
    at: string;
    request_id: string;
    use_case: string | null;
    partner_id: string | null;
    http_status: number;
    outcome: Outcome | string;
    reason: string | null;
    duration_ms: number | null;
    /** Source id → status ("ok", "unavailable", "denied", "error", ...). */
    sources: Record<string, string> | null;
}

/** GET /composite/v1/admin/activity */
export interface ActivityPage {
    /** false: no database configured — nothing is recorded. */
    recording: boolean;
    total: number;
    /** true: the backend stopped counting at `total` (10000), which means "10000 or more". */
    total_capped?: boolean;
    items: ActivityItem[];
}

export interface ActivityFilters {
    partner: string;
    use_case: string;
    outcome: string;
}
