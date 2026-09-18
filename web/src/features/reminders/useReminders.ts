import { useCallback, useEffect, useRef, useState } from "react";
import { ApiClient, errorMessage } from "../../platform/api";
import type { Reminder } from "../../platform/types";

function merge(previous: Reminder[], next: Reminder[]): Reminder[] {
  return [
    ...new Map([...previous, ...next].map((item) => [item.key, item])).values(),
  ];
}

export function useReminders(api: ApiClient) {
  const [items, setItems] = useState<Reminder[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const request = useRef(0);
  const active = useRef<AbortController | null>(null);

  const load = useCallback(
    async (after?: string) => {
      const version = ++request.current;
      active.current?.abort();
      const controller = new AbortController();
      active.current = controller;
      setBusy(true);
      setError("");
      try {
        const page = await api.list(after, controller.signal);
        if (version !== request.current || controller.signal.aborted) return;
        if (page.next_cursor !== null && page.next_cursor === after)
          throw new Error("Pagination did not advance");
        setItems((previous) =>
          after ? merge(previous, page.items) : page.items,
        );
        setCursor(page.next_cursor);
        setLoaded(true);
        setUpdatedAt(new Date());
      } catch (caught) {
        if (!controller.signal.aborted && version === request.current)
          setError(errorMessage(caught));
      } finally {
        if (version === request.current) setBusy(false);
      }
    },
    [api],
  );

  useEffect(() => {
    void load();
    return () => {
      ++request.current;
      active.current?.abort();
    };
  }, [load]);
  const update = useCallback((item: Reminder) => {
    // A request begun before this mutation must not restore its older snapshot.
    ++request.current;
    active.current?.abort();
    setBusy(false);
    setItems((previous) => merge(previous, [item]));
    setUpdatedAt(new Date());
  }, []);

  return {
    items,
    cursor,
    busy,
    error,
    loaded,
    updatedAt,
    update,
    refresh: () => void load(),
    more: () => {
      if (cursor) void load(cursor);
    },
  };
}
