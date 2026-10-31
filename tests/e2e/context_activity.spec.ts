import { test, expect } from "./fixtures";
import { workspaceUrl } from "./auth";
import { mkdirSync, writeFileSync } from "node:fs";

test("Context activity shows changes, decisions and exact links @cross-browser", async ({ page, browser, browserName }, testInfo) => {
  const out = testInfo.outputPath("context-activity");
  mkdirSync(out, {recursive:true});
  const errors: string[] = [];
  const failed: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('response', r => { if (r.status() >= 400) failed.push(`${r.status()} ${new URL(r.url()).pathname}`); });
  await page.addInitScript(() => {
    localStorage.setItem('dtxwiki.uiLanguage', 'ko');
    localStorage.setItem('dtxwiki:workspace-layout:v2:context:left', 'true');
  });
  async function api(method: 'get'|'post'|'patch', url: string, data?: unknown) {
    const r = await page.request[method](url, data === undefined ? {} : {data});
    const text = await r.text();
    expect(r.ok(), `${method} ${url}: ${text.slice(0,600)}`).toBeTruthy();
    return text ? JSON.parse(text).data : undefined;
  }
  await api('post', '/api/v1/auth/signup', {
    email: `context-activity-capture-${Date.now()}@example.com`,
    password: 'correct-password', display_name: 'sangron (검증용)',
  });
  const workspaceData = await api('get','/api/v1/workspaces');
  const wid = workspaceData.current_workspace_id ?? workspaceData.default_workspace_id ?? workspaceData.personal_workspace_id;
  const root = `/api/v1/workspaces/${wid}`;
  let current = await api('post', `${root}/context/pages`, {
    title: '운영 가이드', body_md: '# 운영 가이드\n\n## 배포 전 점검\n\n현재 버전과 서비스 상태를 확인합니다.\n', page_type: 'guide',
  });
  const pid = current.id;
  const pageRoot = `${root}/context/pages/${pid}`;
  const creation = current;
  const reviewDecisions: unknown[]=[];
  async function submit(reason: string) {
    current = await api('post', `${pageRoot}/submit-for-review`, {
      version_id: current.version_id,
      expected_editing_revision: current.editing_revision,
      idempotency_key: `capture-submit-${Date.now()}`, reason,
    });
    return current;
  }
  async function publish(reason: string) {
    reviewDecisions.push(await api('post', `${root}/reviews/${current.review_item_id}/decision`, {
      decision_type:'approve', decision_reason:reason,
    }));
    current = await api('post', `${pageRoot}/versions/${current.version_id}/publish`, {
      reason, expected_content_hash: current.content_hash, publish_despite_findings: true,
    });
    return current;
  }
  const firstSubmitted = await submit('운영 가이드 초안 작성');
  const firstPublished = await publish('운영 가이드 최초 게시');
  current = await api('post', `${pageRoot}/begin-edit`, {idempotency_key: 'capture-begin-second-edit'});
  current = await api('post', `${pageRoot}/autosave`, {
    title: '운영 가이드',
    body_md: '# 운영 가이드\n\n## 배포 전 점검\n\n현재 버전과 서비스 상태를 확인합니다.\n\n## 장애 복구\n\n백업을 확인한 뒤 이전 버전으로 복구하고 상태를 재점검합니다.\n',
    base_version_id: current.version_id, base_content_hash: current.content_hash,
    expected_editing_revision: current.editing_revision,
    session_version_id: current.version_id,
    idempotency_key: 'capture-save-second-edit',
  });
  const secondSaved = current;
  const secondSubmitted = await submit('장애 복구 절차와 점검 항목 보완');
  const secondPublished = await publish('복구 절차 보완본 게시');
  const tree = await api('get', `${root}/tree?section=context`);
  const treeNodes = tree.nodes ?? tree;
  const node = treeNodes.find((n: any) => n.target_id === pid);
  expect(node).toBeTruthy();
  const oldParent = treeNodes.find((n: any) => n.id === node.parent_id);
  await api('patch', `${root}/tree/nodes/${node.id}`, {display_name:'운영 절차 가이드'});
  const folder = await api('post', `${root}/tree/nodes`, {section:'context', title:'서비스 운영'});
  await api('post', `${root}/tree/move`, {node_id:node.id, new_parent_id:folder.id, reason:'운영 문서를 한곳에서 관리'});
  const history = await api('get', `${root}/changesets?target_type=context_page&target_id=${pid}`);
  // Assert metadata from the same snapshot that populates the UI. A separate
  // earlier request can have a different server-clock snapshot boundary.
  const activityResponse = page.waitForResponse(response =>
    response.request().method() === 'GET' &&
    new URL(response.url()).pathname === `${pageRoot}/activity`);
  await page.goto(`${workspaceUrl(wid, `context/pages/${pid}`)}?tab=activity`);
  const response = await activityResponse;
  expect(response.ok()).toBeTruthy();
  const actualFeed = (await response.json()).data;
  writeFileSync(`${out}/actual-api-evidence.json`, JSON.stringify({
    source: 'Actual isolated backend + current SPA; no intercepted AS-IS API responses',
    wid,pid,creation,firstSubmitted,firstPublished,secondSaved,secondSubmitted,secondPublished,
    oldParentTitle:oldParent?.title,folder,actualFeed,history,reviewDecisions,
  },null,2));

  const panel=page.locator('#context-reader-panel-activity');
  await expect(panel.locator('.context-event-card')).toHaveCount(11);
  await expect(panel.locator('.context-event-chip')).toHaveCount(11);
  const moved=panel.locator('[data-action="tree.node.move"]');
  await expect(moved.locator('.context-event-before')).toHaveText('Inbox');
  await expect(moved.locator('.context-event-change strong')).toHaveText('서비스 운영');
  await expect(moved).toContainText('운영 문서를 한곳에서 관리');
  const renamed=panel.locator('[data-action="tree.node.rename"]');
  await expect(renamed.locator('.context-event-before')).toHaveText('운영 가이드');
  await expect(renamed.locator('.context-event-change strong')).toHaveText('운영 절차 가이드');
  await expect(panel.locator('[data-action="review.decision"]')).toHaveCount(2);
  await expect(panel.locator('[data-action="review.decision"] .context-event-chip').first()).toHaveText('승인');
  await expect(panel.locator('[data-action="page.autosave.draft"]')).toContainText('기준 v1');
  expect(actualFeed.items.find((i: any)=>i.body==='page.autosave.draft').details.editing_revision).toBe(secondSaved.editing_revision);
  await expect(panel.locator('[data-action="page.autosave.draft"] a')).toHaveCount(0);
  const submitRows=panel.locator('[data-action="page.submit_context_edit_for_review"]');
  await expect(submitRows.first().getByRole('link',{name:'해당 리뷰 보기'})).toHaveAttribute('href',workspaceUrl(wid, `review/${secondSubmitted.review_item_id}`));
  await page.setViewportSize({width:1440,height:1900});
  await page.locator('.context-reader').screenshot({path:`${out}/implemented-desktop.png`});
  for(const width of [390,320]){
    await page.setViewportSize({width,height:2400});
    await page.waitForFunction(()=>{const n=document.querySelector('.workspace-left-panel');return !n||n.getBoundingClientRect().right<=1;});
    const metrics=await panel.locator('.context-event-card').evaluateAll(cards=>cards.map(card=>{
      const chip=card.querySelector('.context-event-chip')!.getBoundingClientRect();
      const actor=card.querySelector('.context-event-actor')!.getBoundingClientRect();
      return {actorRight:actor.left>=chip.right,overflow:card.scrollWidth-card.clientWidth};
    }));
    expect(metrics.every(m=>m.actorRight&&m.overflow===0)).toBeTruthy();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth)).toBe(0);
    if(width===390)await page.locator('.context-reader').screenshot({path:`${out}/implemented-mobile.png`});
  }
  const author=moved.getByRole('button',{name:/전체 이름/});
  await author.click();
  await expect(page.getByRole('dialog',{name:'활동 기록'})).toContainText('sangron (검증용)');
  await page.keyboard.press('Escape');
  await expect(author).toBeFocused();
  if(browserName==='chromium'){
    const touchContext=await browser.newContext({
      storageState:await page.context().storageState(),viewport:{width:390,height:844},
      hasTouch:true,isMobile:true,baseURL:testInfo.project.use.baseURL,
    });
    const touch=await touchContext.newPage();
    await touch.goto(`${workspaceUrl(wid, `context/pages/${pid}`)}?tab=activity`);
    const touchMove=touch.locator('[data-action="tree.node.move"]');
    await touchMove.getByRole('button',{name:/전체 이름/}).tap();
    await expect(touch.getByRole('dialog',{name:'활동 기록'})).toContainText('sangron (검증용)');
    await touch.getByRole('button',{name:'기록 정보 닫기'}).tap();
    await touchMove.locator('.context-event-paths summary').tap();
    await expect(touchMove.locator('.context-event-paths')).toHaveJSProperty('open',true);
    await touchContext.close();
  }
  await page.setViewportSize({width:1440,height:1100});
  const comparison=panel.locator('[data-action="context_page.draft.publish"]').first().getByRole('link',{name:'변경 내용 비교'});
  await comparison.click();
  await expect(page).toHaveURL(new RegExp(`compareFrom=${firstPublished.version_id}&compareTo=${secondPublished.version_id}`));
  await expect(page.locator('.diff-viewer').first()).toBeVisible();
  await page.goto(`${workspaceUrl(wid, `context/pages/${pid}`)}?tab=activity`);
  await page.locator('[data-action="page.submit_context_edit_for_review"]').first().getByRole('link',{name:'해당 리뷰 보기'}).click();
  await expect(page).toHaveURL(workspaceUrl(wid, `review/${secondSubmitted.review_item_id}`));
  await expect(page.getByRole('heading',{name:'운영 절차 가이드',exact:true}).first()).toBeVisible();
  expect(errors).toEqual([]);
  expect(failed).toEqual([]);
});
