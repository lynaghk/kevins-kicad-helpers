(ns kicad-parser.core-test
  (:require
   [clojure.test :refer [deftest is testing]]
   [datascript.core :as d]
   [kicad-parser.core :as kicad]))

(def sample-netlist
  "(export
     (version \"E\")
     (components
       (comp
         (ref \"C1\")
         (value \"10nF\")
         (footprint \"pio-footprints:CAPC320X160X180L55N\")
         (description \"CAP CER 10000PF 100V C0G 1206\")
         (fields
           (field (name \"Used\") \"Yes\")
           (field (name \"Voltage\") \"100V\")
           (field (name \"Datasheet\")))
         (libsource
           (lib \"pio-dblib\")
           (part \"Capacitors/101-00002\")
           (description \"CAP CER 10000PF 100V C0G 1206\"))
         (property (name \"Used\") (value \"Yes\"))
         (property (name \"Sheetfile\") (value \"eye-spy.kicad_sch\"))
         (sheetpath (names \"/\") (tstamps \"/\"))
         (tstamps \"5f9eda4b-f430-40d8-9c73-60ef2b2cec89\")))
     (nets))")

(deftest parse-components-test
  (is (= [{:component/ref "C1"
           :component/value "10nF"
           :component/footprint "pio-footprints:CAPC320X160X180L55N"
           :component/description "CAP CER 10000PF 100V C0G 1206"
           :component/fields {"Used" "Yes"
                              "Voltage" "100V"
                              "Datasheet" nil}
           :component/properties {"Used" "Yes"
                                  "Sheetfile" "eye-spy.kicad_sch"}
           :component/libsource {:lib "pio-dblib"
                                 :part "Capacitors/101-00002"
                                 :description "CAP CER 10000PF 100V C0G 1206"}
           :component/sheetpath {:names "/"
                                 :tstamps "/"}
           :component/tstamps "5f9eda4b-f430-40d8-9c73-60ef2b2cec89"}]
         (kicad/parse-components sample-netlist))))

(deftest datascript-db-test
  (let [db (kicad/netlist->db sample-netlist)]
    (testing "components are queryable by reference"
      (is (= #{["C1" "10nF"]}
             (d/q '[:find ?ref ?value
                    :where
                    [?e :component/ref ?ref]
                    [?e :component/value ?value]]
                  db))))

    (testing "fields and properties are also attribute entities"
      (is (= #{["field" "Voltage" "100V"]
               ["property" "Sheetfile" "eye-spy.kicad_sch"]}
             (d/q '[:find ?source ?name ?value
                    :where
                    [?component :component/ref "C1"]
                    [?attribute :attribute/component ?component]
                    [?attribute :attribute/source ?source]
                    [?attribute :attribute/name ?name]
                    [?attribute :attribute/value ?value]
                    [(contains? #{"Voltage" "Sheetfile"} ?name)]]
                  db))))))
