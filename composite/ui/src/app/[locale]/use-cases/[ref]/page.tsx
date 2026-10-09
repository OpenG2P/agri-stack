"use client";

import { useParams } from "next/navigation";
import { UseCaseDetailView } from "@/features/use-cases";

export default function UseCaseDetailPage() {
    const { ref } = useParams<{ ref: string }>();
    return <UseCaseDetailView key={ref} useCaseRef={ref} />;
}
