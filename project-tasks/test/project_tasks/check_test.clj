(ns project-tasks.check-test
  (:require [clojure.test :refer [deftest is]]
            [project-tasks.check :as check]))

(deftest check-failed-message-includes-cwd-relative-report
  (is (= "DRC failed. Report: outputs/v1/reports/pump-drc.rpt"
         (check/check-failed-message
          "DRC"
          "/repo/pcbs/pump/outputs/v1/reports/pump-drc.rpt"
          "/repo/pcbs/pump")))
  (is (= "ERC failed. Report: pcbs/pump/outputs/v1/reports/pump-erc.rpt"
         (check/check-failed-message
          "ERC"
          "/repo/pcbs/pump/outputs/v1/reports/pump-erc.rpt"
          "/repo"))))
