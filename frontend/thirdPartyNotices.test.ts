import { inferLicense, packageRoot } from "./thirdPartyNotices";

describe("packageRoot", () => {
  it("resolves a module id to its package root", () => {
    expect(packageRoot("/repo/node_modules/react/index.js")).toBe("/repo/node_modules/react");
  });

  it("keeps the scope of a scoped package", () => {
    expect(packageRoot("/repo/node_modules/@uiw/react-codemirror/esm/index.js")).toBe(
      "/repo/node_modules/@uiw/react-codemirror",
    );
  });

  it("uses the innermost node_modules of a nested dependency", () => {
    expect(packageRoot("/repo/node_modules/mermaid/node_modules/katex/dist/katex.mjs")).toBe(
      "/repo/node_modules/mermaid/node_modules/katex",
    );
  });

  // The desktop release builds on Windows, where asset source paths carry "\\".
  it("matches Windows paths", () => {
    expect(
      packageRoot("C:\\repo\\node_modules\\pdfjs-dist\\build\\pdf.worker.min.mjs"),
    ).toBe("C:/repo/node_modules/pdfjs-dist");
  });

  it("strips the virtual-module prefix and query", () => {
    expect(packageRoot("\0/repo/node_modules/katex/dist/katex.css?inline")).toBe(
      "/repo/node_modules/katex",
    );
  });

  it("returns null for first-party code", () => {
    expect(packageRoot("/repo/frontend/src/main.tsx")).toBeNull();
  });
});

describe("inferLicense", () => {
  // khroma@2.1.0 ships the MIT text but declares no license in package.json.
  it("identifies the MIT permission notice", () => {
    expect(
      inferLicense(
        "The MIT License (MIT)\n\nCopyright (c) 2019-present Someone\n\n" +
          "Permission is hereby granted, free of charge, to any person obtaining a\n" +
          "copy of this software and associated documentation files",
      ),
    ).toBe("MIT");
  });

  it("tells BSD-3-Clause from BSD-2-Clause by the endorsement clause", () => {
    const bsd2 =
      "Redistribution and use in source and binary forms, with or without\n" +
      "modification, are permitted provided that the following conditions are met";
    expect(inferLicense(bsd2)).toBe("BSD-2-Clause");
    expect(inferLicense(`${bsd2}\n\n3. Neither the name of the copyright holder`)).toBe(
      "BSD-3-Clause",
    );
  });

  it("returns null for text it cannot identify", () => {
    expect(inferLicense("Copyright (c) 2020. All rights reserved.")).toBeNull();
  });
});
