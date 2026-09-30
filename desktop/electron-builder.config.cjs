"use strict";

const base = require("./package.json").build;

const repository = process.env.GITHUB_REPOSITORY || "";
const [repositoryOwner, repositoryName] = repository.split("/", 2);
const updateChannel = process.env.DTXWIKI_UPDATE_CHANNEL || "latest";

module.exports = {
  ...base,
  publish:
    repositoryOwner && repositoryName
      ? [
          {
            provider: "github",
            owner: repositoryOwner,
            repo: repositoryName,
            channel: updateChannel,
          },
        ]
      : undefined,
};
