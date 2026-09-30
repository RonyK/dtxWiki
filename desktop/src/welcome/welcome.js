"use strict";

const api = window.dtxwikiDesktop;
const localButton = document.querySelector("#use-local");
const remoteForm = document.querySelector("#remote-form");
const savedSection = document.querySelector("#saved-section");
const savedList = document.querySelector("#saved-servers");
const errorMessage = document.querySelector("#error");

function setBusy(busy) {
  for (const element of document.querySelectorAll("button, input")) {
    element.disabled = busy;
  }
}

function showError(error) {
  const message = error?.message ?? String(error);
  errorMessage.textContent = message.replace(
    /^Error invoking remote method '[^']+':\s*(?:Error:\s*)?/,
    "",
  );
  errorMessage.hidden = false;
  setBusy(false);
}

function savedServerRow(server) {
  const item = document.createElement("li");
  const connect = document.createElement("button");
  connect.type = "button";
  connect.className = "connect";
  const name = document.createElement("strong");
  name.textContent = server.name;
  const url = document.createElement("span");
  url.textContent = server.url;
  connect.append(name, url);
  connect.addEventListener("click", async () => {
    setBusy(true);
    try {
      await api.useRemote(server.id);
    } catch (error) {
      showError(error);
    }
  });

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "remove";
  remove.setAttribute("aria-label", `${server.name} 삭제`);
  remove.textContent = "×";
  remove.addEventListener("click", async () => {
    try {
      render(await api.removeRemote(server.id));
    } catch (error) {
      showError(error);
    }
  });
  item.append(connect, remove);
  return item;
}

function render(state) {
  savedList.replaceChildren(...state.remoteServers.map(savedServerRow));
  savedSection.hidden = state.remoteServers.length === 0;
}

localButton.addEventListener("click", async () => {
  setBusy(true);
  errorMessage.hidden = true;
  try {
    await api.useLocal();
  } catch (error) {
    showError(error);
  }
});

remoteForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setBusy(true);
  errorMessage.hidden = true;
  try {
    await api.addRemote({
      name: document.querySelector("#server-name").value,
      url: document.querySelector("#server-url").value,
    });
  } catch (error) {
    try {
      render(await api.getState());
    } catch {
      // Preserve the original connection error when refreshing state also fails.
    }
    showError(error);
  }
});

api.getState().then(render).catch(showError);
