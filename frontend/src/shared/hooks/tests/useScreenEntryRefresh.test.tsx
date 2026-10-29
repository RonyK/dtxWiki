import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { useAsyncLoad } from "../useAsyncLoad";
import { useScreenEntryRefresh } from "../useScreenEntryRefresh";

function HomeScreen({ load }: { load: (filter: string) => Promise<string> }) {
  const [params] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const filter = params.get("filter") ?? "all";
  const { data, error, reload } = useAsyncLoad(() => load(filter), [filter], {
    errorMessage: "Refresh failed",
    fallbackData: "Empty",
  });
  useScreenEntryRefresh("/items", reload, {
    isHome: !params.has("target"),
    queryDeps: [filter],
    readArrivalNotice: (state) => (state as { arrived?: boolean } | null)?.arrived,
  });
  return (
    <>
      <Link to="/items">Menu</Link>
      <Link to="/items">Breadcrumb</Link>
      <Link to="/items?target=one">Item</Link>
      <Link to="/items?target=one&tab=history">History</Link>
      <button onClick={() => void navigate("/items?expanded=folder", { replace: true })}>Expand</button>
      <Link to="/items?filter=draft">Filter</Link>
      <button onClick={() => void navigate(-1)}>Back</button>
      <button onClick={() => void navigate(location.pathname, { replace: true, state: null })}>
        Normalize address
      </button>
      <output>{data}</output>
      {error ? <p role="alert">{error}</p> : null}
    </>
  );
}

describe("useScreenEntryRefresh", () => {
  it("refreshes home visits from ordinary links and history, without duplicating query loads or address repairs", async () => {
    const load = vi.fn(async (filter: string) => `Loaded ${filter}`);
    render(
      <MemoryRouter initialEntries={[{ pathname: "/items", state: { arrived: true } }]}>
        <HomeScreen load={load} />
      </MemoryRouter>,
    );
    await screen.findByText("Loaded all");
    expect(load).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Normalize address" }));
    expect(load).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("link", { name: "Menu" }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    await userEvent.click(screen.getByRole("link", { name: "Item" }));
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(3));
    await userEvent.click(screen.getByRole("link", { name: "History" }));
    expect(load).toHaveBeenCalledTimes(3);
    // An unavailable detail is repaired with replace; it keeps its error and
    // current collection, rather than counting as another home visit.
    await userEvent.click(screen.getByRole("button", { name: "Normalize address" }));
    expect(load).toHaveBeenCalledTimes(3);
    await userEvent.click(screen.getByRole("link", { name: "History" }));
    await userEvent.click(screen.getByRole("link", { name: "Breadcrumb" }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(4));
    await userEvent.click(screen.getByRole("link", { name: "Menu" }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(5));
    await userEvent.click(screen.getByRole("button", { name: "Expand" }));
    expect(load).toHaveBeenCalledTimes(5);
    await userEvent.click(screen.getByRole("link", { name: "Menu" }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(6));
    await userEvent.click(screen.getByRole("link", { name: "Filter" }));
    await screen.findByText("Loaded draft");
    expect(load).toHaveBeenCalledTimes(7);
    await userEvent.click(screen.getByRole("link", { name: "Menu" }));
    await screen.findByText("Loaded all");
    expect(load).toHaveBeenCalledTimes(8);
  });

  it("discards superseded entry responses and retains loaded rows if refreshing fails", async () => {
    const requests: Array<{ resolve: (value: string) => void; reject: (error: Error) => void }> = [];
    const load = vi.fn(() => new Promise<string>((resolve, reject) => {
      requests.push({ resolve, reject });
    }));
    render(<MemoryRouter initialEntries={["/items"]}><HomeScreen load={load} /></MemoryRouter>);
    await act(async () => requests[0].resolve("Original rows"));
    await userEvent.click(screen.getByRole("link", { name: "Menu" }));
    await userEvent.click(screen.getByRole("link", { name: "Menu" }));
    await act(async () => requests[2].resolve("Newest rows"));
    await act(async () => requests[1].resolve("Stale rows"));
    expect(screen.getByText("Newest rows")).toBeInTheDocument();
    expect(screen.queryByText("Stale rows")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Menu" }));
    await act(async () => requests[3].reject(new Error("offline")));
    expect(screen.getByRole("alert")).toHaveTextContent("Refresh failed");
    expect(screen.getByText("Newest rows")).toBeInTheDocument();
  });
});
