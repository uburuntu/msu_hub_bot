import { useEffect, useRef, useState } from "react";
import type { SetStateAction } from "react";
import { errorMessage } from "../../platform/api";

export function useResource<T>(load: (signal: AbortSignal) => Promise<T>) {
  const previousLoad = useRef(load);
  const active = useRef<AbortController | null>(null);
  const [data, setData] = useState<T>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    if (previousLoad.current !== load) setData(undefined);
    previousLoad.current = load;
    void load(controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setData(result);
      })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(caught));
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [load, revision]);
  return {
    data,
    setData: (next: SetStateAction<T | undefined>) => {
      active.current?.abort();
      setData(next);
      setBusy(false);
      setError("");
    },
    error,
    busy,
    refresh: () => setRevision((value) => value + 1),
  };
}
