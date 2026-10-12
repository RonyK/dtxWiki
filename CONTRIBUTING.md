# Contributing to dtxWiki

dtxWiki is licensed under the GNU Affero General Public License v3.0 only or the
Elastic License 2.0, at the user's election (`AGPL-3.0-only OR Elastic-2.0`),
and dtxSuite also offers it under commercial licenses. The `integrations/`
directory is licensed under Apache License 2.0.

## Contributor License Agreement

Because dtxWiki is distributed under more than one license, and dtxSuite may
offer commercial licenses and operate dtxWiki as a hosted service, dtxSuite
needs the right to use every contribution in each of those ways. Every
contribution is therefore accepted under the dtxWiki Contributor License
Agreement ([CLA.md](CLA.md)). This section is the acceptance process that
section 8 of [CLA.md](CLA.md) refers to, for CLA Version 1. You accept the CLA
once per GitHub account, from the pull request itself:

1. Open the pull request. If your account has not accepted the CLA yet, the
   **CLA** check fails and the bot comments with a link to the agreement.
2. Read the agreement. To accept it, post this acceptance statement yourself,
   from your own account, as a new comment in the pull request's
   conversation, with nothing else in the comment. Letter case and whitespace
   before or after it are ignored. Do not let a bot or an AI agent post it for
   you:

   ```text
   I have read the dtxWiki CLA Version 1 and I hereby sign it
   ```

3. The check re-runs and passes. Later pull requests from the same account
   pass without asking again. If the check stays red, comment `recheck`, or
   close and reopen the pull request. The check reads only the first 30
   comments of a pull request; if yours has more, accept in a new pull request
   or write to <dtxwiki.ai@gmail.com>.

The check refuses a pull request with more than 100 commits, or with a commit
whose author email is not linked to a GitHub account, because it could not
tell who must accept the CLA. Add the email to your GitHub account, or squash
or split the pull request. The account that opens the pull request must also
have accepted the CLA, whoever authored the commits.

If you contribute on behalf of your employer or another legal entity, or if
your employer or anyone else may have rights in the work, give the entity's
legal name and country in the description of every such pull request;
sections 1 and 4 of [CLA.md](CLA.md) describe what you are confirming.
dtxSuite may wait until a person authorized by the entity confirms the CLA by
email from the entity's own domain. If you used a generative AI tool for any
part of the change, name the tool in the pull request description. dtxSuite
does not accept contributions from minors: if the law that applies to you
treats you as a minor (for Korean nationals, under 19), do not accept the CLA.
Pull requests whose authors have not accepted the CLA are not merged, and code
or text posted in an issue, a discussion or an email is not copied into the
Project unless its author has accepted the CLA.

When you accept, the check records in dtxSuite's private repository
`dtxSuite/dtxwiki-cla-signatures`, under `signatures/version1/`, your GitHub
account name and numeric account ID, the pull request number, the ID and
creation time of your comment and the numeric ID of this repository (in
`cla.json`), and a copy of your comment and of the pull request description,
taken when the check processed your comment or, if a retry was needed, as soon
as it could afterwards (the file says which), with the SHA-256 of the CLA text
you were shown (in `evidence/<comment ID>.json`). The `version1` directory
shows that you
accepted CLA Version 1. The record is not public; section 8 of
[CLA.md](CLA.md) says who may see it and how long dtxSuite keeps it. GitHub,
Inc. stores it, possibly outside your country. You may decline to accept the
CLA, but then dtxSuite cannot merge your contribution. Editing or deleting
your comment later does not withdraw your acceptance. To get a copy of your
record or have it corrected, write to <dtxwiki.ai@gmail.com>. The bot links to
CLA Version 1 at this repository's `cla-v1` tag, which is protected against
deletion and change. dtxSuite announces any change to this process in the
release notes or `CHANGELOG.md`, as section 8 of [CLA.md](CLA.md) describes.

### 한글 안내

아래는 [CLA.md](CLA.md)(영문 본문)의 핵심을 한글로 요약한 안내입니다. 이해를
돕기 위한 것이며 본문을 바꾸지 않습니다.

- 저작권은 기여자에게 남습니다. 양도가 아니라 dtxSuite(김민수와 그 승계인)에
  대한 이용허락입니다.
- 이용허락은 영구적·전 세계적·비독점적·무상이며 철회할 수 없습니다(법이
  배제를 허용하지 않는 해지권은 예외). dtxSuite는 기여물을 AGPL, Elastic
  License 2.0 같은 라이선스뿐 아니라 상용·비공개 라이선스로도 제공하고,
  호스팅·SaaS로 운영할 수 있습니다.
- 기여에 대한 보상, 로열티, 수익 배분은 없습니다.
- 특허는 기여물이 필연적으로 침해하는 범위에서 실시를 허락합니다. 기여물이나
  프로젝트가 특허를 침해한다고 소송을 제기한 자에 대한 허락은 소송 제기일에
  종료됩니다.
- 저작인격권은 유지되지만, 프로젝트의 유지·개발·배포·운영에 필요한 수정에는
  이의를 제기하지 않습니다. 명예를 훼손하는 방식의 수정·이용은 제외됩니다.
  릴리스에 포함된 기여는 기여자 목록이나 릴리스 노트에 이름을 표시합니다.
- 기여자는 기여물이 본인의 창작물이고, 생성형 AI를 썼다면 도구를 밝히고 그
  약관을 지키며 결과물을 검토했고, 알고 있는 제3자 라이선스와 제약을 알렸으며,
  미성년자가 아니라고 진술합니다. 회사 업무로 기여하면 회사의 허락이 있어야
  하고, 회사 정식 명칭과 국가를 pull request 설명에 적습니다.
- 동의는 위 절차대로 pull request에 수락 문구를 직접 남겨서 합니다. pull
  request를 연 계정도 동의해야 합니다. 기록(GitHub 계정명·계정 번호·시각·pull
  request 번호·댓글 사본 등)은 비공개 저장소에 보관되며, 사본 제공과 정정은
  <dtxwiki.ai@gmail.com>으로 요청할 수 있습니다.
- 준거법은 대한민국 법이고, 서울중앙지방법원이 합의된 제1심 관할
  법원입니다(다른 관할 법원을 배제하지 않습니다).

### For maintainers

The check is [`.github/workflows/cla.yml`](.github/workflows/cla.yml). It
writes to the private repository with a short-lived token from a GitHub App
that is installed only on that repository and may only read and write its
contents: repository variable `CLA_APP_CLIENT_ID`, and secret
`CLA_APP_PRIVATE_KEY` of the environment `cla-signatures`, whose only
deployment branch is `main`, so a workflow on another branch cannot read the
key. The bot commits to the private repository's `main` directly, so protect
that branch only against deletion and force pushes. Pull requests opened by
bots fail the check by design; a maintainer reviews such a change and merges
it through the ruleset bypass.

Before the first pull request, a maintainer tags the published text and
creates the record once, from a clone of this repository, after the final
CLA.md and this file are on `main` on GitHub. First tag the text the bot links
to, in your clone only; the tag cannot be changed once it is pushed:

```sh
git fetch origin &&
git cat-file -e origin/main:CLA.md &&
git cat-file -e origin/main:CONTRIBUTING.md &&
git tag -a cla-v1 -m "dtxWiki CLA v1, published $(date -u +%F)" origin/main
```

Then create `dtxSuite/dtxwiki-cla-signatures` on GitHub as an empty private
repository and seed it. The steps stop at the first failure and change nothing
in your clone; if one fails, delete the directory in `$seed` and run them
again:

```sh
seed=$(mktemp -d) && v1="$seed/signatures/version1" &&
mkdir -p "$v1" &&
git show cla-v1:CLA.md > "$v1/CLA.md" &&
git show cla-v1:CONTRIBUTING.md > "$v1/CONTRIBUTING.md" &&
(cd "$v1" && sha256sum CLA.md CONTRIBUTING.md > SHA256SUMS) &&
printf '{\n  "signedContributors": []\n}\n' > "$v1/cla.json" &&
git -C "$seed" init -q -b main &&
git -C "$seed" add signatures &&
git -C "$seed" commit -q -m "chore(cla): seed CLA v1 records" &&
git -C "$seed" push git@github.com:dtxSuite/dtxwiki-cla-signatures.git main
```

Until the next command the tag exists only in your clone; if it points at the
wrong commit, remove it with `git tag -d cla-v1` and start again. Once the
record exists, protect `cla-*` tags against deletion and update with a tag
ruleset that has no bypass, then publish the tag:

```sh
git push origin refs/tags/cla-v1
```

The private repository must start with these four files: the copy of CLA
Version 1, the copy of this file that describes the acceptance process,
`SHA256SUMS` for both copies, and `cla.json` containing
`{"signedContributors": []}`, because the action cannot create a missing file.
The check adds `evidence/<comment ID>.json` files next to them. The `cla-v1`
tag and the commit that adds these files are the fixed reference for
Version 1. Never edit the copies or move the tag. When this process changes (a
new CONTRIBUTING.md, or a change to `.github/workflows/cla.yml` in how
acceptance is checked or recorded), tag the `main` commit that carries the
change as `cla-v1-YYYY-MM-DD`, add the CONTRIBUTING.md of that commit to
`signatures/version1/` in the private repository as
`CONTRIBUTING-YYYY-MM-DD.md`, and append its SHA-256 to `SHA256SUMS`. A new
CLA version gets a new `cla-vN` tag, a new `signatures/versionN` directory, a
new acceptance statement and new acceptances.

The maintainer does not accept their own CLA. The check reads both the pull
request author's account and every commit author's linked GitHub account
before using the signature repository. Only a pull request whose participants
are all exempt skips signature processing; contributions from any other
account still require acceptance.

## Development setup and tests

[DEVELOPMENT.md](DEVELOPMENT.md) covers the checkout setup (Python 3.12,
Node.js 22.18 or newer), the local infrastructure, and how to run the lint,
backend, frontend and end-to-end tests. Run `make lint` and the relevant tests
before opening a pull request.

## Reporting security vulnerabilities

Do not open a public issue for a vulnerability. Follow
[.github/SECURITY.md](.github/SECURITY.md) to report it privately.
