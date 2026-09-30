import { apiErrorMessage } from "../api/client";
import { isAbortError } from "../lib/abortError";

export type UploadQueueStatus =
  | "queued"
  | "uploading"
  | "succeeded"
  | "failed"
  | "cancelled";

export type ResilientUploadQueueStatus =
  | UploadQueueStatus
  | "cancelling"
  | "outcome-unknown";

type QueueStatus<ActiveCancelOutcomeUnknown extends boolean> =
  ActiveCancelOutcomeUnknown extends true
    ? ResilientUploadQueueStatus
    : UploadQueueStatus;

export type UploadQueueEntry<
  T,
  Status extends ResilientUploadQueueStatus = UploadQueueStatus,
> = {
  attempt: number;
  error?: unknown;
  id: string;
  message?: string;
  retryAt?: number;
  status: Status;
  value: T;
};

export type UploadQueueAdapter<
  T,
  Status extends ResilientUploadQueueStatus = UploadQueueStatus,
> = {
  upload(value: T, signal: AbortSignal): Promise<void>;
  errorMessage?(error: unknown): string;
  onChange?(entry: UploadQueueEntry<T, Status>): void;
  retryDelayMs?(error: unknown, attempt: number): number | null;
};

export type UploadQueueOptions<ActiveCancelOutcomeUnknown extends boolean = false> = {
  activeCancelOutcomeUnknown?: ActiveCancelOutcomeUnknown;
  maxConcurrency?: number;
  now?: () => number;
  schedule?: (callback: () => void, delayMs: number) => ReturnType<typeof setTimeout>;
  unschedule?: (handle: ReturnType<typeof setTimeout>) => void;
};

type InternalEntry<T> = UploadQueueEntry<T, ResilientUploadQueueStatus> & {
  controller?: AbortController;
  settle?: () => void;
  timer?: ReturnType<typeof setTimeout>;
};

/**
 * A small, framework-independent, sequential upload state machine.
 * Domain validation and success effects belong in the injected adapter.
 */
export class UploadQueue<T, ActiveCancelOutcomeUnknown extends boolean = false> {
  private readonly entries = new Map<string, InternalEntry<T>>();
  private readonly pendingIds: string[] = [];
  private activeCount = 0;
  private maxConcurrency: number;
  private readonly now: () => number;
  private readonly schedule: NonNullable<UploadQueueOptions["schedule"]>;
  private readonly unschedule: NonNullable<UploadQueueOptions["unschedule"]>;
  private readonly activeCancelOutcomeUnknown: boolean;

  constructor(
    private readonly adapter: UploadQueueAdapter<T, QueueStatus<ActiveCancelOutcomeUnknown>>,
    options: UploadQueueOptions<ActiveCancelOutcomeUnknown> = {},
  ) {
    this.activeCancelOutcomeUnknown = options.activeCancelOutcomeUnknown === true;
    this.maxConcurrency = normalizedConcurrency(options.maxConcurrency);
    this.now = options.now ?? Date.now;
    this.schedule = options.schedule ?? ((callback, delayMs) => setTimeout(callback, delayMs));
    this.unschedule = options.unschedule ?? ((handle) => clearTimeout(handle));
  }

  enqueue(id: string, value: T): Promise<void> {
    if (this.entries.has(id)) {
      throw new Error(`Upload queue entry already exists: ${id}`);
    }
    return this.queue({ attempt: 0, id, value, status: "queued" });
  }

  retry(id: string): Promise<void> | null {
    const entry = this.entries.get(id);
    if (!entry || !isRetryableStatus(entry.status)) {
      return null;
    }
    const delayMs = Math.max(0, (entry.retryAt ?? this.now()) - this.now());
    return this.queue({
      ...entry,
      controller: undefined,
      error: undefined,
      message: undefined,
      settle: undefined,
      status: "queued",
      timer: undefined,
    }, delayMs);
  }

  retryFailed(): Promise<void>[] {
    const retries: Promise<void>[] = [];
    for (const entry of this.entries.values()) {
      if (entry.status !== "failed") continue;
      const retry = this.retry(entry.id);
      if (retry) retries.push(retry);
    }
    return retries;
  }

  cancel(id: string): boolean {
    const entry = this.entries.get(id);
    if (!entry || (entry.status !== "queued" && entry.status !== "uploading")) {
      return false;
    }
    if (entry.status === "queued") {
      this.clearTimer(entry);
      this.transition(entry, "cancelled");
      entry.settle?.();
      entry.settle = undefined;
      return true;
    }
    entry.controller?.abort();
    if (this.activeCancelOutcomeUnknown) {
      this.transition(entry, "cancelling");
      this.transition(entry, "outcome-unknown");
    } else {
      this.transition(entry, "cancelled");
    }
    entry.settle?.();
    entry.settle = undefined;
    return true;
  }

  cancelAll(): void {
    for (const id of this.entries.keys()) {
      this.cancel(id);
    }
  }

  remove(id: string): boolean {
    const entry = this.entries.get(id);
    if (!entry || !isRemovableStatus(entry.status)) {
      return false;
    }
    this.clearTimer(entry);
    this.entries.delete(id);
    return true;
  }

  clear(): void {
    this.cancelAll();
    for (const entry of this.entries.values()) {
      this.clearTimer(entry);
    }
    this.entries.clear();
    this.pendingIds.length = 0;
  }

  setMaxConcurrency(maxConcurrency: number): void {
    this.maxConcurrency = normalizedConcurrency(maxConcurrency);
    this.pump();
  }

  snapshot(): UploadQueueEntry<T, QueueStatus<ActiveCancelOutcomeUnknown>>[] {
    return Array.from(
      this.entries.values(),
      ({ controller: _controller, settle: _settle, timer: _timer, ...entry }) => entry,
    ) as UploadQueueEntry<T, QueueStatus<ActiveCancelOutcomeUnknown>>[];
  }

  private queue(entry: InternalEntry<T>, delayMs = 0): Promise<void> {
    let settle: (() => void) | undefined;
    const completion = new Promise<void>((resolve) => {
      settle = resolve;
    });
    const queued: InternalEntry<T> = {
      ...entry,
      controller: undefined,
      retryAt: delayMs > 0 ? this.now() + delayMs : undefined,
      settle,
      timer: undefined,
    };
    this.entries.set(entry.id, queued);
    this.emit(queued);
    if (delayMs > 0) {
      queued.timer = this.schedule(() => {
        queued.timer = undefined;
        queued.retryAt = undefined;
        if (this.entries.get(queued.id) !== queued || queued.status !== "queued") return;
        this.pendingIds.push(queued.id);
        this.pump();
      }, delayMs);
    } else {
      this.pendingIds.push(entry.id);
      this.pump();
    }
    return completion;
  }

  private pump(): void {
    while (this.activeCount < this.maxConcurrency && this.pendingIds.length > 0) {
      const id = this.pendingIds.shift();
      if (!id) continue;
      const entry = this.entries.get(id);
      if (!entry || entry.status !== "queued") continue;
      this.activeCount += 1;
      void this.run(entry);
    }
  }

  private async run(entry: InternalEntry<T>): Promise<void> {
    const controller = new AbortController();
    entry.controller = controller;
    entry.attempt += 1;
    this.transition(entry, "uploading");
    try {
      await this.adapter.upload(entry.value, controller.signal);
      this.transition(
        entry,
        controller.signal.aborted
          ? this.activeCancelOutcomeUnknown ? "outcome-unknown" : "cancelled"
          : "succeeded",
      );
    } catch (error) {
      if (controller.signal.aborted || isAbortError(error)) {
        this.transition(
          entry,
          this.activeCancelOutcomeUnknown ? "outcome-unknown" : "cancelled",
        );
      } else {
        const retryDelayMs = normalizedRetryDelay(
          this.adapter.retryDelayMs?.(error, entry.attempt),
        );
        entry.error = error;
        entry.retryAt = retryDelayMs === null ? undefined : this.now() + retryDelayMs;
        this.transition(
          entry,
          "failed",
          this.adapter.errorMessage?.(error) ?? defaultErrorMessage(error),
        );
      }
    } finally {
      entry.controller = undefined;
      entry.settle?.();
      entry.settle = undefined;
      this.activeCount = Math.max(0, this.activeCount - 1);
      this.pump();
    }
  }

  private transition(
    entry: InternalEntry<T>,
    status: ResilientUploadQueueStatus,
    message?: string,
  ): void {
    if (this.entries.get(entry.id) !== entry) return;
    if ((entry.status === "cancelled" || entry.status === "outcome-unknown") &&
        status !== "queued") return;
    if (entry.status === status && entry.message === message) return;
    entry.status = status;
    entry.message = message;
    if (status !== "failed" && status !== "queued") {
      entry.error = undefined;
      entry.retryAt = undefined;
    }
    this.emit(entry);
  }

  private emit(entry: InternalEntry<T>): void {
    const {
      controller: _controller,
      settle: _settle,
      timer: _timer,
      ...publicEntry
    } = entry;
    this.adapter.onChange?.(
      publicEntry as UploadQueueEntry<T, QueueStatus<ActiveCancelOutcomeUnknown>>,
    );
  }

  private clearTimer(entry: InternalEntry<T>) {
    if (entry.timer === undefined) return;
    this.unschedule(entry.timer);
    entry.timer = undefined;
  }
}

function isRetryableStatus(status: ResilientUploadQueueStatus) {
  return status === "failed" || status === "cancelled" || status === "outcome-unknown";
}

function isRemovableStatus(status: ResilientUploadQueueStatus) {
  return status === "failed" || status === "cancelled" || status === "outcome-unknown";
}

function normalizedConcurrency(value: number | undefined) {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : 1;
}

function normalizedRetryDelay(value: number | null | undefined) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? Math.trunc(value)
    : null;
}

function defaultErrorMessage(error: unknown): string {
  return apiErrorMessage(error);
}
