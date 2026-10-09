"use client";

import LoadingState from "@/components/LoadingState";
import {
    createContext,
    useCallback,
    useContext,
    useMemo,
    useState,
    useEffect,
    type ReactNode,
} from "react";

import { useAuth } from "@/context/Authcontext";

interface RbacContextType {
    loading: boolean;
    actions: string[];
    can: (action: string) => boolean;
    canAny: (actionList: readonly string[]) => boolean;
    canAll: (actionList: readonly string[]) => boolean;
    refresh: () => Promise<void>;
}

const RbacContext = createContext<RbacContextType | null>(null);

export function RbacProvider({ children }: { children: ReactNode }) {
    const { isLoggedIn, handleUnauthorized } = useAuth();
    const [loading, setLoading] = useState(true);
    const [actionSet, setActionSet] = useState<Set<string>>(new Set());

    /** Permissions of the user in this application (APPLICATION_MNEMONIC), or null after a 401. */
    const fetchActions = useCallback(async (): Promise<Set<string> | null> => {
        if (!isLoggedIn) return new Set();
        try {
            const res = await fetch("/api/permissions", { cache: "no-store" });
            if (res.status === 401) {
                handleUnauthorized();
                return null;
            }
            const data: unknown = await res.json();
            const permissions = Array.isArray(data)
                ? data.flatMap((app: { permissions?: string[] }) => app.permissions || [])
                : [];
            return new Set<string>(permissions);
        } catch {
            return new Set();
        }
    }, [isLoggedIn, handleUnauthorized]);

    const loadActions = useCallback(async () => {
        setLoading(true);
        const actions = await fetchActions();
        if (actions) setActionSet(actions);
        setLoading(false);
    }, [fetchActions]);

    useEffect(() => {
        let cancelled = false;
        fetchActions().then((actions) => {
            if (cancelled) return;
            if (actions) setActionSet(actions);
            setLoading(false);
        });
        return () => {
            cancelled = true;
        };
    }, [fetchActions]);

    const can = useCallback(
        (action: string) => actionSet.has(action),
        [actionSet]
    );

    const canAny = useCallback(
        (actionList: readonly string[]) => actionList.some((a) => actionSet.has(a)),
        [actionSet]
    );

    const canAll = useCallback(
        (actionList: readonly string[]) => actionList.every((a) => actionSet.has(a)),
        [actionSet]
    );

    const value = useMemo<RbacContextType>(
        () => ({
            loading,
            actions: Array.from(actionSet),
            can,
            canAny,
            canAll,
            refresh: loadActions,
        }),
        [loading, actionSet, can, canAny, canAll, loadActions]
    );

    if (loading) {
        return <LoadingState fullScreen />;
    }

    return (
        <RbacContext.Provider value={value}>
            {children}
        </RbacContext.Provider>
    );
}

export function useRbac() {
    const context = useContext(RbacContext);
    if (!context) {
        throw new Error("useRbac must be used inside <RbacProvider>");
    }
    return context;
}
