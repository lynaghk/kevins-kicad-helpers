(ns kicad-analyzer.core
  (:require
   [clojure.edn :as edn]
   [clojure.java.io :as io]
   clojure.pprint
   [clojure.set :as set]
   [clojure.string :as str]
   [datascript.core :as d]))


(defmethod print-method datascript.impl.entity.Entity [e ^java.io.Writer w]
  (.write w (pr-str (into {:db/id (:db/id e)} (d/touch e)))))

(defmethod clojure.pprint/simple-dispatch datascript.impl.entity.Entity [e]
  (clojure.pprint/simple-dispatch (into {} (d/touch e))))

(def schema
  {:symbol/id {:db/unique :db.unique/identity}
   :instance/ref {:db/unique :db.unique/identity}
   :instance/symbol {:db/valueType :db.type/ref}
   :instance/attributes {:db/valueType :db.type/ref
                         :db/cardinality :db.cardinality/many
                         :db/isComponent true}
   :instance/pins {:db/valueType :db.type/ref
                   :db/cardinality :db.cardinality/many
                   :db/isComponent true}
   :net/name {:db/unique :db.unique/identity}
   :net/nodes {:db/valueType :db.type/ref
               :db/cardinality :db.cardinality/many
               :db/isComponent true}
   :node/pin {:db/valueType :db.type/ref}})

(defn- tagged?
  [tag node]
  (and (seq? node)
       (= tag (first node))))

(defn- child
  [tag node]
  (first (filter #(tagged? tag %) (rest node))))

(defn- children
  [tag node]
  (filter #(tagged? tag %) (rest node)))

(defn- child-value
  [tag node]
  (second (child tag node)))

(defn- named-value
  [node]
  [(child-value 'name node)
   (child-value 'value node)])

(defn- field-value
  [node]
  [(child-value 'name node)
   (first (filter string? (rest node)))])

(defn- entries
  [entry-fn nodes]
  (into {}
        (keep (fn [node]
                (let [[k v] (entry-fn node)]
                  (when k
                    [k v]))))
        nodes))

(defn- libsource
  [component]
  (when-let [source (child 'libsource component)]
    {:lib (child-value 'lib source)
     :part (child-value 'part source)
     :description (child-value 'description source)}))

(defn- sheetpath
  [component]
  (when-let [path (child 'sheetpath component)]
    (child-value 'names path)))

(defn- symbol-id
  [{:keys [lib part]}]
  (str lib ":" part))

(defn- symbol-entity
  [node]
  (when-let [source (libsource node)]
    (cond-> {:symbol/id (symbol-id source)
             :symbol/lib (:lib source)
             :symbol/part (:part source)}
      (:description source) (assoc :symbol/description (:description source)))))

(def ^:private ignored-attribute-names
  #{"Description"
    "Footprint"
    "Sheetfile"
    "Sheetname"
    "ki_fp_filters"
    "ki_keywords"})

(defn- parse-hex
  [s]
  (Long/parseLong (str/replace (str/trim s) #"(?i)^0x" "") 16))

(defn parse-i2c-address
  "Parse an I2C address field value into a sorted set of the 7-bit addresses it
   covers. Accepts a single hex address (\"0x48\") or an inclusive hex range
   (\"0x10..0x17\"). Returns nil for blank/nil input. Returning a set means a
   single part and a range-addressed part compose the same way, e.g. checking
   that two parts don't overlap is just (empty? (set/intersection a b))."
  [value]
  (when-let [s (some-> value str/trim not-empty)]
    (if-let [[_ from to] (re-matches #"(.+)\.\.(.+)" s)]
      (into (sorted-set) (range (parse-hex from)
                                (parse-hex to)))
      (sorted-set (parse-hex s)))))

(def ^:private attribute-parsers
  "Per-attribute value parsers applied once at import time, so queries can use
   the typed value directly instead of re-parsing the raw string each time."
  {"max_mA" parse-double
   "i2c"    parse-i2c-address})

(defn- attribute
  [[name value]]
  (when (some? value)
    (let [parse (attribute-parsers name)]
      (if-some [parsed (if parse (parse value) value)]
        {:attribute/name name
         :attribute/value parsed}
        (binding [*out* *err*]
          (println (format "Skipping unparseable attribute %s=%s" name (pr-str value))))))))

(defn- attributes
  [node]
  (let [fields-node (child 'fields node)]
    (->> (concat
          (entries field-value (children 'field fields-node))
          (entries named-value (children 'property node)))
         (remove (fn [[name _value]]
                   (contains? ignored-attribute-names name)))
         (keep attribute)
         distinct
         vec)))

(defn- net-node
  [node]
  {:node/ref (child-value 'ref node)
   :node/pin-number (child-value 'pin node)})

(defn- net-node-pin
  [node]
  (cond-> {:pin/number (child-value 'pin node)}
    (child-value 'pinfunction node) (assoc :pin/function (child-value 'pinfunction node))
    (child-value 'pintype node) (assoc :pin/type (child-value 'pintype node))))

(defn- pins-by-ref
  [nets-node]
  (->> (children 'net nets-node)
       (mapcat #(children 'node %))
       (group-by #(child-value 'ref %))
       (map (fn [[ref nodes]]
              [ref (->> nodes
                        (map net-node-pin)
                        (distinct)
                        (sort-by :pin/number)
                        vec)]))
       (into {})))

(defn- instance
  [pins-by-ref node]
  (let [source (libsource node)
        sheetpath (sheetpath node)]
    (cond-> {:instance/ref (child-value 'ref node)
             :instance/value (child-value 'value node)
             :instance/attributes (attributes node)
             :instance/pins (get pins-by-ref (child-value 'ref node) [])}
      (child-value 'footprint node) (assoc :instance/footprint (child-value 'footprint node))
      (child-value 'description node) (assoc :instance/description (child-value 'description node))
      source (assoc :instance/symbol [:symbol/id (symbol-id source)])
      sheetpath (assoc :instance/sheetpath sheetpath))))

(defn- net
  [node]
  {:net/name (child-value 'name node)
   :net/nodes (mapv net-node (children 'node node))})

(defn parse-netlist
  [netlist-text]
  (let [netlist (edn/read-string netlist-text)
        components-node (child 'components netlist)
        component-nodes (children 'comp components-node)
        nets-node (child 'nets netlist)
        pins-by-ref (pins-by-ref nets-node)]
    {:symbols (vec (keep symbol-entity component-nodes))
     :instances (mapv #(instance pins-by-ref %) component-nodes)
     :nets (mapv net (children 'net nets-node))}))

(defn parse-components
  [netlist-text]
  (:instances (parse-netlist netlist-text)))

(defn- base-tx
  [{:keys [symbols instances]}]
  (concat
   (keep identity symbols)
   instances))

(defn instance-pin
  [db instance-ref pin-number]
  (when-let [pin-id (d/q '[:find ?pin .
                           :in $ ?ref ?pin-number
                           :where
                           [?instance :instance/ref ?ref]
                           [?instance :instance/pins ?pin]
                           [?pin :pin/number ?pin-number]]
                         db instance-ref pin-number)]
    (d/entity db pin-id)))

(defn- resolved-net-node
  [db {:keys [node/ref node/pin-number]}]
  (if-let [pin (instance-pin db ref pin-number)]
    {:node/pin (:db/id pin)}
    (throw (ex-info "Net references an unknown instance pin"
                    {:instance-ref ref
                     :pin-number pin-number}))))

(defn- net-tx
  [db {:net/keys [name nodes]}]
  {:net/name name
   :net/nodes (mapv #(resolved-net-node db %) nodes)})

(defn netlist-data->db
  [netlist-data]
  (let [conn (d/create-conn schema)]
    (d/transact! conn (base-tx netlist-data))
    (d/transact! conn (mapv #(net-tx @conn %) (:nets netlist-data)))
    @conn))

(defn components->db
  [components]
  (netlist-data->db {:symbols []
                     :instances components
                     :nets []}))

(defn netlist->db
  [netlist-text]
  (netlist-data->db (parse-netlist netlist-text)))

(defn i2c-addresses
  [db]
  (d/q '{:find [?hex-addr (distinct ?ref)]
         :where [[?instance :instance/ref ?ref]
                 [?instance :instance/attributes ?attribute]
                 [?attribute :attribute/name "i2c"]
                 [?attribute :attribute/value ?addrs]
                 [(clojure.core/identity ?addrs) [?addr ...]]
                 [(clojure.core/format "0x%x" ?addr) ?hex-addr]]}
       db))


(defn check-i2c!
  [db]
  (let [refs-by-addr (i2c-addresses db)]

    (clojure.pprint/print-table (sort-by :addr (for [[addr refs] refs-by-addr]
                                                 {:addr addr :refs (clojure.string/join " "  (sort refs))})))

    (doseq [[addr refs] refs-by-addr
            :when (< 1 (count refs))]
      (throw (ex-info (str "Addr " addr " matches multiple refs: " refs))))))


(def ^:private capacitance-multipliers
  {"p" 1e-12 "n" 1e-9 "u" 1e-6 "µ" 1e-6 "m" 1e-3 "" 1.0})

(defn parse-capacitance
  "Parse a capacitor value string like \"100nF\"/\"1uF\"/\"4.7µF\" into farads.
   Returns nil for blank/nil/unparseable input."
  [s]
  (when s
    (when-let [[_ n u] (re-matches #"(?i)\s*([0-9.]+)\s*([pnuµm]?)f?\s*" s)]
      (* (Double/parseDouble n) (capacitance-multipliers (str/lower-case u))))))

(defn net-capacitance
  "Total capacitance (farads) of every C* capacitor with a pin on `net-name`.
   Values are parsed from each capacitor's :instance/value (see parse-capacitance)."
  [db net-name]
  (some->> (d/q '{:find  [?ref ?v]
                  :in    [$ ?net]
                  :where [[?n :net/name ?net]
                          [?n :net/nodes ?node]
                          [?node :node/pin ?pin]
                          [?i :instance/pins ?pin]
                          [?i :instance/ref ?ref]
                          [(clojure.string/starts-with? ?ref "C")]
                          [?i :instance/value ?v]]}
                db net-name)
           (keep (comp parse-capacitance second))
           seq
           (reduce + 0.0)))


(defn check-total-capacitance!
  [db]
  (let [rows (->> ["VCC" "VBUS"] ;;TODO: make this configurable
                  (keep (fn [net]
                          (when-let [c (net-capacitance db net)]
                            {:net net :total-uF (format "%.2f" (* c 1e6))}))))]

    (clojure.pprint/print-table rows)

    (doseq [{:keys [net total-uF]} rows]
      (assert (< (Double/parseDouble total-uF) 10) (str "Net " net " exceeds USB spec 10uF capacitance")))))


(defn- executable?
  [path]
  (when path
    (let [file (io/file path)]
      (and (.exists file)
           (.canExecute file)
           (.getPath file)))))

(defn- run-command
  [command]
  (let [process (-> (ProcessBuilder. command)
                    (.redirectErrorStream true)
                    (.start))
        out (slurp (.getInputStream process))
        exit (.waitFor process)]
    {:exit exit
     :out out}))

(defn kicad-cli-command
  []
  (let [os-name (str/lower-case (System/getProperty "os.name"))
        mac-cli (executable? "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")]
    (if (and (str/includes? os-name "mac") mac-cli)
      [mac-cli]
      ["kicad-cli"])))

(defn export-netlist
  [schematic-path]
  (let [schematic-file (io/file schematic-path)
        output-dir (or (.getParentFile (.getAbsoluteFile schematic-file))
                       (io/file "."))
        out-file (java.io.File/createTempFile "kicad-parser-" ".net" output-dir)
        command (into (kicad-cli-command)
                      ["sch" "export" "netlist"
                       "--format" "kicadsexpr"
                       "--output" (.getPath out-file)
                       schematic-path])
        {:keys [exit out]} (run-command command)]
    (try
      (when-not (zero? exit)
        (throw (ex-info "KiCad netlist export failed"
                        {:command command
                         :exit exit
                         :output out})))
      (slurp out-file)
      (finally
        (.delete out-file)))))

(defn schematic->db
  [schematic-path]
  (netlist->db (export-netlist schematic-path)))


(defn instances
  [db]
  (->> (d/q '{:find [[?eid ...]]
              :where [[?eid :instance/ref _]]}
            db)
       (map (partial d/entity db))))


(defn instance-attribute
  ([instance k]
   (->> (:instance/attributes instance)
        (keep (fn [{:attribute/keys [name value]}]
                (when (= name k)
                  value)))
        first))

  ([instance k not-found]
   (if-some [v (instance-attribute instance k)]
     v
     not-found)))

(defn instance-part
  [instance]
  (-> instance :instance/symbol :symbol/part))


(defn check-power!
  [db]
  (let [rows  (->> (d/q '{:find  [?part ?mA (count ?i)]
                          :where [[?i :instance/ref ?ref]
                                  ;; [(clojure.string/starts-with? ?ref "U")]
                                  [?i :instance/symbol ?sym]
                                  [?sym :symbol/part ?part]
                                  [?i :instance/attributes ?attr]
                                  [?attr :attribute/name "max_mA"]
                                  [?attr :attribute/value ?mA]]}
                        db)
                   (sort-by first)
                   (map (fn [[part mA cnt]]
                          {:part part
                           :count cnt
                           :mA (format "%.2f" mA)
                           :total (format "%.2f" (* cnt mA))})))
        table-str (with-out-str (clojure.pprint/print-table [:part :count :mA :total] rows))
        table-width (->> (clojure.string/split table-str  #"\n") second count)
        total-ma (reduce + 0.0 (map #(Double/parseDouble (:total %)) rows))]

    ;; TODO: make this configurable
    (assert (<= total-ma 300))

    (print table-str)
    (println (apply str (repeat table-width "-")))
    (println (format "Total: %.2f mA" total-ma))))



;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;; Reporting


(defn check-schematic!
  [schematic]
  (let [db (schematic->db schematic)]

    (print "ic power")
    (check-power! db)

    (println "")
    (print "i2c addresses")
    (check-i2c! db)

    (println "")
    (println "total capacitance:")
    (check-total-capacitance! db)

    ;;
    ))



;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;; REPL exploration underneath

(comment

  (check-schematic!  "../plate-reader/pcbs/receiver/receiver.kicad_sch")

  (def db
    (schematic->db "../plate-reader/pcbs/receiver/receiver.kicad_sch"))

  (def db
    (schematic->db "../plate-reader/pcbs/emitter/emitter.kicad_sch"))

  (i2c-addresses db)

  (d/touch (d/entity db [:instance/ref "U1"]))

  (->> (instances db)
       (filter #(.startsWith (:instance/ref %) "U"))
       (sort-by instance-part)
       (map (fn [i]
              {:part (instance-part i)
               :mA (instance-attribute i "max_mA")}))
       clojure.pprint/print-table)

  ;; Current draw grouped by part, with instance count and total (count x mA).
  ;; Uncomment the starts-with clause to restrict to "U" refs.

  (instance-attribute (first (instances db))
                      "max_mA")

  ;;All instances connected to a net (net -> nodes -> pin -> owning instance).
  (->> (d/q '[:find [?instance ...]
              :in $ ?net-namen
              :where
              [?net :net/name ?net-name]
              [?net :net/nodes ?node]
              [?node :node/pin ?pin]
              [?instance :instance/pins ?pin]]
            db "/scl")
       (map #(d/entity db %)))

  ;; Total capacitance (farads) on a power net: sums every C* capacitor with a
  ;; pin on the net, parsing values like "100nF"/"1uF" to farads.
  (net-capacitance db "VCC")
  ;; => 6.41e-6
  (into (sorted-map)
        (for [net ["VCC" "VBUS" "foooo"]]
          [net (format "%.3f µF" (* 1e6 (net-capacitance db net)))]))
  ;; => {"VBUS" "0.000 µF", "VCC" "6.410 µF"}

;;
  )
