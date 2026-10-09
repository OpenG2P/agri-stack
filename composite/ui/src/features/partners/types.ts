import type { PortalLinks } from "@/features/overview/types";

export interface PartnerKey {
    kid: string;
    algorithm: string;
}

/** One item of GET /composite/v1/admin/partners ("*" = any partner, shown as its own row). */
export interface Partner {
    partner_id: string;
    pm_reference: string | null;
    use_cases: string[];
    /** null when Partner Management could not be read (see pm_error). */
    pm_keys: PartnerKey[] | null;
    pm_error: string | null;
}

export interface PartnerList {
    partners: Partner[];
    links: PortalLinks;
}
