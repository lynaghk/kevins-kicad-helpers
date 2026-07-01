#!/usr/bin/env bb
(require '[babashka.fs :as fs]
         '[babashka.process :as p])

(p/shell {:dir (str (-> *file* fs/canonicalize fs/parent fs/parent))
          :inherit true}
         "cljfmt"
         "--no-remove-consecutive-blank-lines"
         "fix"
         "project-tasks/src")
