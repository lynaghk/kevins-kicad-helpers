(ns project-tasks.build-test
  (:require [babashka.fs :as fs]
            [clojure.string :as str]
            [clojure.test :refer [deftest is]]
            [project-tasks.build :as build]))

(defn with-temp-tree [f]
  (let [root (fs/create-temp-dir)]
    (try
      (f root)
      (finally
        (fs/delete-tree root)))))

(def positions-header "Designator,Mid X,Mid Y,Rotation,Layer")

(defn spit-positions [root & rows]
  (spit (fs/file (fs/path root "positions.csv"))
        (str "﻿" positions-header "\r\n"
             (apply str (map #(str % "\r\n") rows)))))

(defn positions-lines [root]
  (-> (slurp (str (fs/path root "positions.csv")))
      (str/replace-first "﻿" "")
      str/split-lines))

(deftest filter-positions-drops-rows-missing-from-bom
  (with-temp-tree
    (fn [root]
      (spit (fs/file (fs/path root "bom.csv"))
            "﻿Designator,Footprint,Quantity,Value,LCSC Part #\r\n\"R1, R2\",R_0402,2,10k,C25744\r\n")
      (spit-positions root
                      "R1,0.0,0.0,0.0,top"
                      "H1,5.0,5.0,0.0,top"
                      "R2,1.0,1.0,0.0,top")
      (build/filter-positions-to-bom! root)
      (is (= [positions-header
              "R1,0.0,0.0,0.0,top"
              "R2,1.0,1.0,0.0,top"]
             (positions-lines root))))))

(deftest filter-positions-empties-rows-when-bom-missing
  (with-temp-tree
    (fn [root]
      (spit-positions root
                      "E1,0.0,0.0,0.0,top"
                      "J1,0.0,6.25,0.0,top")
      (build/filter-positions-to-bom! root)
      (is (= [positions-header] (positions-lines root))))))
