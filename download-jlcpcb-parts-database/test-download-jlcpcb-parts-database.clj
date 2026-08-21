#!/usr/bin/env bb
;; End-to-end tests for the parts database transform.
;; A handmade jlc_components fixture runs through the real generated SQL with the
;; real sqlite3 binary; assertions check the resulting side tables and components rows.

(require '[babashka.fs :as fs]
         '[babashka.process :as p]
         '[cheshire.core :as json]
         '[clojure.string :as str]
         '[clojure.test :as t :refer [deftest is testing]])

(load-file (str (fs/path (fs/parent (fs/canonicalize *file*)) "download-jlcpcb-parts-database.clj")))

;; ---- fixture ----

(def fixture-rows
  [{:lcsc       1001                                                                :category "Resistors" :library_type "base"
    :price      "1-199:0.0189,200-599:0.0163"
    :attributes {"Resistance"          "10kΩ" "Tolerance"               "±1%"       "Power(Watts)" "1/4W"
                 "Voltage-Supply(Max)" "50V"  "Temperature Coefficient" "±100ppm/℃"}}
   {:lcsc       1002                                         :category "Resistors"
    :attributes {"Resistance" "10mΩ" "Power(Watts)" "1/16W"}}
   {:lcsc       1003                                     :category "Resistors"
    :attributes {"Resistance" "1MΩ" "Tolerance" "±0.1%"}}
   {:lcsc       1004                                                             :category "Capacitors"
    :attributes {"Capacitance"             "100nF" "Tolerance" "±10%" "Voltage Rating" "50V"
                 "Temperature Coefficient" "X7R"}}
   {:lcsc       1005                                           :category "Capacitors"
    :attributes {"Capacitance"             "0.1uF"      "Tolerance" "-20%~+80%"
                 "Temperature Coefficient" "-55℃~+125℃"}}
   ;; Blank category rescued via the description.
   {:lcsc       1006                       :category "" :description "Multilayer Ceramic Capacitors MLCC - SMD/SMT 100000pF"
    :attributes {"Capacitance" "100000pF"}}
   {:lcsc       1007                 :category "" :description "Thick Film Chip Resistor 10Ω 5%"
    :attributes {"Resistance" "10Ω"}}
   ;; A potentiometer has a resistance attribute but must stay out of `resistors`.
   {:lcsc       1008                  :category "" :description "Trimmer Potentiometer 10kΩ Top Adjustment"
    :attributes {"Resistance" "10kΩ"}}
   {:lcsc 1009 :category "Connectors" :attributes {}}
   {:lcsc       1010                                                            :category "Inductors (SMD)"
    :attributes {"Inductance"         "10uH" "Tolerance" "±20%" "Current Rating" "1.2A"
                 "DC Resistance(DCR)" "52mΩ"}}
   ;; Excluded: out of stock / not present.
   {:lcsc 1011 :category "Resistors" :stock 0 :attributes {"Resistance" "1kΩ"}}
   {:lcsc 1012 :category "Resistors" :present 0 :attributes {"Resistance" "1kΩ"}}
   ;; Preferred counts as non-extended; malformed price becomes NULL.
   {:lcsc 1013 :category "Connectors" :preferred 1 :price "garbage"}
   ;; Upstream writes "8MΩ" on power-inductor DCR where it means mΩ.
   {:lcsc       1014                                            :category "Inductors (SMD)"
    :attributes {"Inductance" "1uH" "DC Resistance(DCR)" "8MΩ"}}])

(def row-defaults
  {:category ""  :subcategory "" :mfr       "MFR-X" :manufacturer "Maker"  :package   "0402"
   :joints   2   :description "" :datasheet ""      :library_type "expand" :preferred 0
   :stock    100 :present     1  :price     ""})

(declare insert-sql q build-fixture-db!)

(def generated-at "2026-01-01T00:00:00Z")

(def dbs
  ;; Build once; every test reads from the result.
  (delay
    (let [dir     (fs/create-temp-dir {:prefix "kkh-jlcpcb-test-"})
          fixture (str (fs/path dir "fixture.sqlite3"))
          out-db  (str (fs/path dir "out.db"))]
      (-> (Runtime/getRuntime)
          (.addShutdownHook (Thread. #(fs/delete-tree dir))))
      (build-fixture-db! fixture)
      (p/shell {:in (build-sql {:new-file out-db :generated-at generated-at})}
               "sqlite3" fixture)
      {:fixture fixture :out-db out-db})))

(defn build-fixture-db! [fixture]
  (let [ddl (str "CREATE TABLE jlc_components ("
                 "lcsc INTEGER PRIMARY KEY, category TEXT, subcategory TEXT, "
                 "mfr TEXT, manufacturer TEXT, package TEXT, joints INTEGER, "
                 "description TEXT, datasheet TEXT, library_type TEXT, preferred INTEGER, "
                 "stock INTEGER, present INTEGER, price TEXT, attributes TEXT);\n"
                 "CREATE TABLE lcsc_components (lcsc INTEGER PRIMARY KEY);\n")]
    (p/shell {:in (str ddl (str/join "\n" (map insert-sql fixture-rows)))}
             "sqlite3" fixture)))

(defn insert-sql [row]
  (let [{:keys [lcsc category subcategory mfr manufacturer package joints description
                datasheet library_type preferred stock present price attributes]}
        (merge row-defaults row)
        s                                                                             #(str "'" (sql-quote %) "'")]
    (str "INSERT INTO jlc_components VALUES ("
         (str/join ", " [lcsc (s category) (s subcategory) (s mfr) (s manufacturer)
                         (s package) joints (s description) (s datasheet)
                         (s library_type) preferred stock present (s price)
                         (s (json/generate-string attributes))])
         ");")))

(defn q [sql]
  (-> (p/shell {:out :string :err :string} "sqlite3" (:out-db @dbs) sql)
      :out str/trim))

;; ---- tests ----

(deftest components-table
  (testing "row filtering"
    (is (= "12" (q "SELECT COUNT(*) FROM components")))
    (is (= "0" (q "SELECT COUNT(*) FROM components WHERE lcsc IN (1011, 1012)"))))
  (testing "extended flag"
    (is (= "0" (q "SELECT extended FROM components WHERE lcsc = 1001")))
    (is (= "1" (q "SELECT extended FROM components WHERE lcsc = 1002")))
    (is (= "0" (q "SELECT extended FROM components WHERE lcsc = 1013"))))
  (testing "price: first tier rounded; malformed or empty becomes NULL"
    (is (= "1" (q "SELECT price = 0.019 FROM components WHERE lcsc = 1001")))
    (is (= "1" (q "SELECT price IS NULL FROM components WHERE lcsc = 1002")))
    (is (= "1" (q "SELECT price IS NULL FROM components WHERE lcsc = 1013"))))
  (testing "empty attributes become NULL"
    (is (= "1" (q "SELECT attributes IS NULL FROM components WHERE lcsc = 1009")))))

(deftest resistors-table
  (testing "unit suffixes parse case-sensitively"
    (is (= "1" (q "SELECT ohms = 10000.0 FROM resistors WHERE lcsc = 1001")))
    (is (= "1" (q "SELECT ohms = 0.01 FROM resistors WHERE lcsc = 1002")))
    (is (= "1" (q "SELECT ohms = 1000000.0 FROM resistors WHERE lcsc = 1003"))))
  (testing "tolerance, power, voltage, tempco"
    (is (= "1" (q "SELECT tolerance_pct = 1.0 FROM resistors WHERE lcsc = 1001")))
    (is (= "1" (q "SELECT tolerance_pct = 0.1 FROM resistors WHERE lcsc = 1003")))
    (is (= "1" (q "SELECT power_w = 0.25 FROM resistors WHERE lcsc = 1001")))
    (is (= "1" (q "SELECT power_w = 0.0625 FROM resistors WHERE lcsc = 1002")))
    (is (= "1" (q "SELECT voltage_v = 50.0 FROM resistors WHERE lcsc = 1001")))
    (is (= "1" (q "SELECT tempco_ppm = 100.0 FROM resistors WHERE lcsc = 1001"))))
  (testing "membership: blank category rescued via description; potentiometer stays out"
    (is (= "1" (q "SELECT COUNT(*) FROM resistors WHERE lcsc = 1007")))
    (is (= "0" (q "SELECT COUNT(*) FROM resistors WHERE lcsc = 1008")))))

(deftest capacitors-table
  (testing "100nF, 0.1uF, and 100000pF canonicalize to the identical double"
    (is (= "3" (q "SELECT COUNT(*) FROM capacitors WHERE farads = 100e-9")))
    (is (= "1" (q "SELECT COUNT(DISTINCT farads) FROM capacitors"))))
  (testing "asymmetric tolerance becomes NULL"
    (is (= "1" (q "SELECT tolerance_pct = 10.0 FROM capacitors WHERE lcsc = 1004")))
    (is (= "1" (q "SELECT tolerance_pct IS NULL FROM capacitors WHERE lcsc = 1005"))))
  (testing "dielectric kept only when it looks like a dielectric class"
    (is (= "X7R" (q "SELECT dielectric FROM capacitors WHERE lcsc = 1004")))
    (is (= "1" (q "SELECT dielectric IS NULL FROM capacitors WHERE lcsc = 1005")))))

(deftest inductors-table
  (is (= "1" (q "SELECT henries = 10e-6 FROM inductors WHERE lcsc = 1010")))
  (is (= "1" (q "SELECT tolerance_pct = 20.0 FROM inductors WHERE lcsc = 1010")))
  (is (= "1" (q "SELECT current_a = 1.2 FROM inductors WHERE lcsc = 1010")))
  (is (= "1" (q "SELECT dcr_ohms = 0.052 FROM inductors WHERE lcsc = 1010")))
  (testing "mislabeled MΩ DCR reads as milliohms"
    (is (= "1" (q "SELECT dcr_ohms = 0.008 FROM inductors WHERE lcsc = 1014")))))

(deftest schema-and-meta
  (testing "all expected indexes exist"
    (is (= "10" (q "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"))))
  (testing "meta table records provenance"
    (is (= generated-at (q "SELECT value FROM meta WHERE key = 'generated_at'")))
    (is (= "12" (q "SELECT value FROM meta WHERE key = 'row_count'")))
    (is (= (str base-url "/cache.zip") (q "SELECT value FROM meta WHERE key = 'source_url'")))))

(deftest plan-summary-lines
  (let [lines (plan-summary {:part-names  ["cache.z01" "cache.zip"]
                             :total-bytes 500000000
                             :work-dir    "/scratch/x"
                             :output      "db.sqlite"
                             :existing    {:generated-at "2026-01-01T00:00:00Z" :row-count "42"}})]
    (is (str/includes? (nth lines 1) "download 2 files, ~500 MB total"))
    (is (str/includes? (nth lines 2) "~5 GB of scratch space in /scratch/x"))
    (is (str/includes? (nth lines 3) "replace the database at db.sqlite (built 2026-01-01T00:00:00Z, 42 parts)")))
  (is (str/includes? (nth (plan-summary {:part-names ["cache.zip"] :total-bytes 1
                                         :work-dir   "w"           :output      "o" :existing {:generated-at nil}})
                          3)
                     "not a recognized parts database"))
  (is (str/includes? (nth (plan-summary {:part-names ["cache.zip"] :total-bytes 1
                                         :work-dir   "w"           :output      "o" :existing nil})
                          3)
                     "write the finished database to o")))

(let [{:keys [fail error]} (t/run-tests 'user)]
  (System/exit (if (zero? (+ fail error)) 0 1)))
