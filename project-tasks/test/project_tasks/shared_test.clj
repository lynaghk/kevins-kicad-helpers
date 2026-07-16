(ns project-tasks.shared-test
  (:require [babashka.fs :as fs]
            [babashka.process]
            [cheshire.core :as json]
            [clojure.string :as str]
            [clojure.test :refer [deftest is]]
            [project-tasks.shared :as shared]))

(defn with-temp-tree [f]
  (let [root (fs/create-temp-dir)]
    (try
      (f root)
      (finally
        (fs/delete-tree root)))))

(defn git-init! [root]
  (let [{:keys [exit err]} @(babashka.process/process ["git" "init"]
                                                       {:dir (str root)
                                                        :out :string
                                                        :err :string})]
    (when-not (zero? exit)
      (throw (ex-info err {})))))

(defn same-path? [expected actual]
  (= (str (fs/canonicalize expected))
     (some-> actual fs/canonicalize str)))

(deftest find-repo-dir-in-git-root
  (with-temp-tree
    (fn [root]
      (git-init! root)
      (is (same-path? root (shared/find-repo-dir root))))))

(deftest find-repo-dir-walks-up-from-subdir
  (with-temp-tree
    (fn [root]
      (git-init! root)
      (fs/create-dirs (fs/path root "hardware" "some-board"))
      (fs/create-dirs (fs/path root "firmware" "src"))
      (is (same-path? root
                      (shared/find-repo-dir (fs/path root "hardware" "some-board"))))
      (is (same-path? root
                      (shared/find-repo-dir (fs/path root "firmware" "src")))))))

(deftest find-repo-dir-prefers-nearest-git-ancestor
  (with-temp-tree
    (fn [root]
      (let [inner (fs/path root "nested-project")]
        (git-init! root)
        (fs/create-dirs inner)
        (git-init! inner)
        (fs/create-dirs (fs/path inner "docs"))
        (is (same-path? inner
                        (shared/find-repo-dir (fs/path inner "docs"))))))))

(deftest find-repo-dir-matches-symlinked-start-dir
  (with-temp-tree
    (fn [root]
      (let [real-root (fs/path root "real-project")
            linked-root (fs/path root "linked-project")]
        (fs/create-dirs (fs/path real-root "docs"))
        (git-init! real-root)
        (fs/create-sym-link linked-root real-root)
        (is (same-path? linked-root
                        (shared/find-repo-dir (fs/path linked-root "docs"))))))))

(deftest find-repo-dir-nil-when-outside-git
  (with-temp-tree
    (fn [root]
      (fs/create-dirs (fs/path root "src"))
      (is (nil? (shared/find-repo-dir (fs/path root "src")))))))

(deftest with-text-variable-defines-variable-during-body
  (with-temp-tree
    (fn [root]
      (let [project-file (fs/path root "board.kicad_pro")]
        (spit (fs/file project-file) "{\"meta\": {\"version\": 3}}")
        (shared/with-text-variable
          project-file "KKH_VERSION_DATE" "2026-07-07-393e14-dirty"
          #(is (= "2026-07-07-393e14-dirty"
                  (get-in (json/parse-string (slurp (fs/file project-file)))
                          ["text_variables" "KKH_VERSION_DATE"]))))
        (is (= "{\"meta\": {\"version\": 3}}" (slurp (fs/file project-file))))))))

(deftest with-board-text-variable-updates-cached-property-during-body
  (with-temp-tree
    (fn [root]
      (let [pcb (fs/path root "board.kicad_pcb")
            original (str "(kicad_pcb\n"
                          "\t(property \"KKH_VERSION_DATE\" \"0000-00-00-unbuilt\")\n"
                          "\t(gr_text \"${KKH_VERSION_DATE}\")\n"
                          ")\n")]
        (spit (fs/file pcb) original)
        (shared/with-board-text-variable
          pcb "KKH_VERSION_DATE" "2026-07-07-393e14-dirty"
          #(is (str/includes?
                (slurp (fs/file pcb))
                "(property \"KKH_VERSION_DATE\" \"2026-07-07-393e14-dirty\")")))
        (is (= original (slurp (fs/file pcb))))))))

(deftest with-board-text-variable-leaves-board-without-property-alone
  (with-temp-tree
    (fn [root]
      (let [pcb (fs/path root "board.kicad_pcb")
            original "(kicad_pcb\n\t(gr_text \"${KKH_VERSION_DATE}\")\n)\n"]
        (spit (fs/file pcb) original)
        (shared/with-board-text-variable
          pcb "KKH_VERSION_DATE" "2026-07-07-393e14"
          #(is (= original (slurp (fs/file pcb)))))
        (is (= original (slurp (fs/file pcb))))))))

(deftest with-board-text-variable-restores-file-on-exception
  (with-temp-tree
    (fn [root]
      (let [pcb (fs/path root "board.kicad_pcb")
            original "(kicad_pcb\n\t(property \"KKH_VERSION_DATE\" \"old\")\n)\n"]
        (spit (fs/file pcb) original)
        (is (thrown? Exception
                     (shared/with-board-text-variable
                       pcb "KKH_VERSION_DATE" "new"
                       #(throw (ex-info "boom" {})))))
        (is (= original (slurp (fs/file pcb))))))))

(deftest ensure-text-variable-defines-missing-variable
  (with-temp-tree
    (fn [root]
      (let [project-file (fs/path root "board.kicad_pro")]
        (spit (fs/file project-file) "{\"meta\": {\"version\": 3}}")
        (shared/ensure-text-variable! project-file "KKH_VERSION_DATE" "0000-00-00-unbuilt")
        (is (= "0000-00-00-unbuilt"
               (get-in (json/parse-string (slurp (fs/file project-file)))
                       ["text_variables" "KKH_VERSION_DATE"])))))))

(deftest ensure-text-variable-keeps-existing-value
  (with-temp-tree
    (fn [root]
      (let [project-file (fs/path root "board.kicad_pro")
            original "{\"text_variables\": {\"KKH_VERSION_DATE\": \"custom\"}}"]
        (spit (fs/file project-file) original)
        (shared/ensure-text-variable! project-file "KKH_VERSION_DATE" "0000-00-00-unbuilt")
        (is (= original (slurp (fs/file project-file))))))))

(deftest with-text-variable-restores-file-on-exception
  (with-temp-tree
    (fn [root]
      (let [project-file (fs/path root "board.kicad_pro")]
        (spit (fs/file project-file) "{\"text_variables\": {\"OTHER\": \"kept\"}}")
        (is (thrown? Exception
                     (shared/with-text-variable
                       project-file "KKH_VERSION_DATE" "value"
                       #(throw (ex-info "boom" {})))))
        (is (= "{\"text_variables\": {\"OTHER\": \"kept\"}}"
               (slurp (fs/file project-file))))))))
