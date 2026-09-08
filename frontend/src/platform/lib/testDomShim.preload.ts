// `bunfig.toml`'s `[test] preload` entry: the one place the DOM shim gets
// installed early enough to cover a plain static import of a module that
// reads `window`/`location`/`history` at module scope. See testDomShim.ts
// for why that ordering is the whole point.
//
// Side effect only, no exports — bun evaluates a preload module for its
// effects before it loads any test file.
import { installDomShim } from "./testDomShim";

installDomShim();
