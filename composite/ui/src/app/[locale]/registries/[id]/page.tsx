"use client";

import { useParams } from "next/navigation";
import { RegistryDetailView } from "@/features/registries";

export default function RegistryDetailPage() {
    const { id } = useParams<{ id: string }>();
    return <RegistryDetailView key={id} registryId={decodeURIComponent(id)} />;
}
