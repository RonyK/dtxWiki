import { LoadingIndicator } from "./LoadingIndicator";
import { RouteLoading } from "./RouteLoading";
import { SearchField } from "./SearchField";

export default {
  title: "Shared/LoadingIndicator",
  component: LoadingIndicator,
  args: { children: "워크스페이스 불러오는 중…" },
};

export const Inline = {};

export const LongLabel = {
  render: () => (
    <div style={{ maxWidth: 240 }}>
      <LoadingIndicator>
        선택한 워크스페이스에 접근할 수 있는 앱과 연결 정보를 불러오는 중입니다…
      </LoadingIndicator>
    </div>
  ),
};

export const Page = {
  render: () => <RouteLoading label="설정 불러오는 중…" />,
};

export const SearchButton = {
  render: () => (
    <SearchField
      ariaLabel="워크스페이스 검색"
      clearLabel="검색 지우기"
      loading
      onChange={() => {}}
      submitLabel="검색"
      value="Design"
    />
  ),
};
