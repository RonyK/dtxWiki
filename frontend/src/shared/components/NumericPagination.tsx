type NumericPaginationLabels = {
  navigation: string;
  previous: string;
  next: string;
  page: (page: number) => string;
  currentPage: (page: number) => string;
  perPage: string;
  range: (start: number, end: number, total: number) => string;
};

type NumericPaginationProps = {
  page: number;
  pageCount: number;
  pageSize: number;
  pageSizeOptions?: readonly number[];
  total: number;
  labels: NumericPaginationLabels;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
};

type PageToken = number | "leading-gap" | "trailing-gap";

function pageTokens(page: number, pageCount: number): PageToken[] {
  if (pageCount <= 7) {
    return Array.from({ length: pageCount }, (_, index) => index + 1);
  }
  const visible = new Set([1, pageCount, page - 1, page, page + 1]);
  const pages = Array.from(visible)
    .filter((value) => value >= 1 && value <= pageCount)
    .sort((a, b) => a - b);
  const result: PageToken[] = [];
  pages.forEach((value, index) => {
    const previous = pages[index - 1];
    if (previous && value - previous > 1) {
      result.push(previous === 1 ? "leading-gap" : "trailing-gap");
    }
    result.push(value);
  });
  return result;
}

/** Board-style numeric pagination with an exact result range. */
export function NumericPagination({
  page,
  pageCount,
  pageSize,
  pageSizeOptions = [20, 50, 100],
  total,
  labels,
  onPageChange,
  onPageSizeChange,
}: NumericPaginationProps) {
  const start = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const end = total === 0 ? 0 : Math.min(page * pageSize, total);

  return (
    <nav aria-label={labels.navigation} className="numeric-pagination">
      <span className="numeric-pagination__range">
        {labels.range(start, end, total)}
      </span>
      {pageCount > 1 ? (
        <div className="numeric-pagination__pages">
          <button
            className="icon-button text-button"
            disabled={page <= 1}
            onClick={() => onPageChange(page - 1)}
            type="button"
          >
            {labels.previous}
          </button>
          {pageTokens(page, pageCount).map((token) =>
            typeof token === "number" ? (
              <button
                aria-current={token === page ? "page" : undefined}
                aria-label={
                  token === page
                    ? labels.currentPage(token)
                    : labels.page(token)
                }
                className={`numeric-pagination__page${token === page ? " is-current" : ""}`}
                key={token}
                onClick={() => onPageChange(token)}
                type="button"
              >
                {token}
              </button>
            ) : (
              <span aria-hidden="true" className="numeric-pagination__gap" key={token}>
                …
              </span>
            ),
          )}
          <button
            className="icon-button text-button"
            disabled={page >= pageCount}
            onClick={() => onPageChange(page + 1)}
            type="button"
          >
            {labels.next}
          </button>
        </div>
      ) : null}
      {pageSizeOptions.length > 1 ? (
        <label className="numeric-pagination__size">
          <span>{labels.perPage}</span>
          <select
            aria-label={labels.perPage}
            onChange={(event) => onPageSizeChange(Number(event.target.value))}
            value={pageSize}
          >
            {pageSizeOptions.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
      ) : null}
    </nav>
  );
}
