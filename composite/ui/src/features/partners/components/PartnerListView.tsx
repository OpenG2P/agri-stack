"use client";

import { useTranslations } from "next-intl";
import DataTable, { type DataTableColumn } from "@/components/DataTable";
import QueryView from "@/components/QueryView";
import TableSkeleton from "@/components/TableSkeleton";
import { ChipList, PageHeader } from "@/components/ui";
import { PortalLinks } from "@/features/overview/components/PortalLinks";
import { toUseCaseHref } from "@/features/use-cases/api";
import { ANY_PARTNER } from "../api";
import { usePartners } from "../hooks";
import type { Partner } from "../types";

function PmKeys({ partner }: { partner: Partner }) {
    const t = useTranslations();
    if (partner.pm_error) return <span className="break-words text-[13px] text-red-700">{partner.pm_error}</span>;
    if (partner.pm_keys === null) return <span className="text-gray-500">—</span>;
    if (partner.pm_keys.length === 0) return <span className="text-amber-700">{t("no_pm_keys")}</span>;
    return (
        <ul className="space-y-1">
            {partner.pm_keys.map((k) => (
                <li key={k.kid} className="text-[13px]">
                    <span className="break-all font-mono">{k.kid}</span>
                    <span className="ml-2 rounded bg-gray-100 px-1.5 py-0.5 font-mono text-[12px] text-gray-700">{k.algorithm}</span>
                </li>
            ))}
        </ul>
    );
}

export default function PartnerListView() {
    const t = useTranslations();
    const query = usePartners();

    const columns: DataTableColumn<Partner>[] = [
        {
            key: "partner",
            header: t("col_partner_id"),
            render: (p) =>
                p.partner_id === ANY_PARTNER ? (
                    <span title={t("any_partner_hint")}>
                        <span className="font-mono font-semibold">*</span>
                        <span className="ml-2 text-[13px] text-gray-600">{t("any_partner")}</span>
                    </span>
                ) : (
                    <span className="font-mono font-medium">{p.partner_id}</span>
                ),
        },
        { key: "pm_reference", header: t("col_pm_reference"), render: (p) => <span className="font-mono text-[13px]">{p.pm_reference || "—"}</span> },
        {
            key: "use_cases",
            header: t("nav_use_cases"),
            render: (p) => <ChipList items={p.use_cases} mono hrefOf={toUseCaseHref} />,
        },
        { key: "pm_keys", header: t("col_pm_keys"), render: (p) => <PmKeys partner={p} /> },
    ];

    return (
        <div className="space-y-6">
            <PageHeader
                title={t("nav_partners")}
                hint={t("partners_hint")}
                actions={<PortalLinks links={query.data?.links} />}
            />
            <QueryView query={query} loading={<TableSkeleton columns={4} rows={4} />}>
                {(data) => (
                    <DataTable
                        columns={columns}
                        rows={data.partners}
                        rowKey={(p) => p.partner_id}
                        searchText={(p) => [p.partner_id, p.pm_reference ?? "", ...p.use_cases].join(" ")}
                        emptyMessage={t("no_partners")}
                    />
                )}
            </QueryView>
        </div>
    );
}
