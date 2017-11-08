;;
;; Copyright (c) Two Sigma Open Source, LLC
;;
;; Licensed under the Apache License, Version 2.0 (the "License");
;; you may not use this file except in compliance with the License.
;; You may obtain a copy of the License at
;;
;;  http://www.apache.org/licenses/LICENSE-2.0
;;
;; Unless required by applicable law or agreed to in writing, software
;; distributed under the License is distributed on an "AS IS" BASIS,
;; WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
;; See the License for the specific language governing permissions and
;; limitations under the License.
;;
(ns cook.test.mesos.sandbox
  (:use clojure.test)
  (:require [clj-http.client :as http]
            [clojure.core.cache :as cache]
            [clojure.string :as str]
            [cook.mesos.sandbox :as sandbox]
            [cook.test.testutil :as tu]
            [datomic.api :as d]
            [metrics.counters :as counters]
            [plumbing.core :as pc])
  (:import (java.util.concurrent CountDownLatch TimeUnit)))

(deftest test-agent->task-id->sandbox
  (let [task-id->sandbox-agent (agent {})]
    (send task-id->sandbox-agent sandbox/aggregate-sandbox "t0" "s0")
    (send task-id->sandbox-agent sandbox/aggregate-sandbox "t2" "s2")
    (send task-id->sandbox-agent sandbox/aggregate-sandbox "t3" "s3")
    (await task-id->sandbox-agent)

    (is (= "s0" (sandbox/agent->task-id->sandbox task-id->sandbox-agent "t0")))
    (is (nil? (sandbox/agent->task-id->sandbox task-id->sandbox-agent "t1")))
    (is (= "s2" (sandbox/agent->task-id->sandbox task-id->sandbox-agent "t2")))
    (is (= "s3" (sandbox/agent->task-id->sandbox task-id->sandbox-agent "t3")))
    (is (nil? (sandbox/agent->task-id->sandbox task-id->sandbox-agent "t4")))))

(defmacro assert-clear-agent-state-result
  [initial-state clear-data expected-state]
  `(let [task-id->sandbox-agent# (agent ~initial-state)]
     (counters/clear! sandbox/sandbox-aggregator-pending-count)
     (counters/inc! sandbox/sandbox-aggregator-pending-count (count ~initial-state))

     (send task-id->sandbox-agent# sandbox/clear-agent-state ~clear-data)
     (await task-id->sandbox-agent#)

     (is (= @task-id->sandbox-agent# ~expected-state))
     (is (= (counters/value sandbox/sandbox-aggregator-pending-count) (count ~expected-state)))))

(deftest test-clear-agent-state
  (assert-clear-agent-state-result nil nil nil)
  (assert-clear-agent-state-result nil {} nil)
  (assert-clear-agent-state-result nil {"a" 1} nil)

  (assert-clear-agent-state-result {} nil {})
  (assert-clear-agent-state-result {} {} {})
  (assert-clear-agent-state-result {"a" 1, "b" 2} {"a" 1, "b" 2} {})

  (assert-clear-agent-state-result {"a" 1} nil {"a" 1})
  (assert-clear-agent-state-result {"a" 1} {} {"a" 1})

  (assert-clear-agent-state-result {"a" 1, "b" 2} {"c" 3, "d" 4} {"a" 1, "b" 2})
  (assert-clear-agent-state-result {"a" 1, "b" 2, "c" 3, "d" 4} {"c" 3, "d" 4} {"a" 1, "b" 2}))

(defmacro assert-aggregate-sandbox-single
  [initial-state task-id sandbox expected-state]
  `(let [task-id->sandbox-agent# (agent ~initial-state)]
     (counters/clear! sandbox/sandbox-aggregator-pending-count)
     (counters/inc! sandbox/sandbox-aggregator-pending-count (count ~initial-state))

     (send task-id->sandbox-agent# sandbox/aggregate-sandbox ~task-id ~sandbox)
     (await task-id->sandbox-agent#)

     (is (= @task-id->sandbox-agent# ~expected-state))
     (is (= (counters/value sandbox/sandbox-aggregator-pending-count) (count ~expected-state)))))

(deftest test-aggregate-sandbox-single
  (assert-aggregate-sandbox-single {} "a" 1 {"a" 1})
  (assert-aggregate-sandbox-single {"a" 1} "a" 2 {"a" 1})
  (assert-aggregate-sandbox-single {"x" 7, "y" 8, "z" 9} "a" 1 {"a" 1, "x" 7, "y" 8, "z" 9}))

(defmacro assert-aggregate-sandbox-multiple
  [initial-state aggregate-data expected-state]
  `(let [task-id->sandbox-agent# (agent ~initial-state)]
     (counters/clear! sandbox/sandbox-aggregator-pending-count)
     (counters/inc! sandbox/sandbox-aggregator-pending-count (count ~initial-state))

     (send task-id->sandbox-agent# sandbox/aggregate-sandbox ~aggregate-data)
     (await task-id->sandbox-agent#)

     (is (= @task-id->sandbox-agent# ~expected-state))
     (is (= (counters/value sandbox/sandbox-aggregator-pending-count) (count ~expected-state)))))

(deftest test-aggregate-sandbox-multiple
  (assert-aggregate-sandbox-multiple {} {"a" 1} {"a" 1})
  (assert-aggregate-sandbox-multiple {} {"a" 1, "b" 2, "c" 2} {"a" 1, "b" 2, "c" 2})
  (assert-aggregate-sandbox-multiple {"a" 1} {"a" 2} {"a" 1})
  (assert-aggregate-sandbox-multiple {"x" 7, "y" 8} {"a" 1, "x" 7} {"a" 1, "x" 7, "y" 8})
  (assert-aggregate-sandbox-multiple {"x" 7, "y" 8} {"a" 1, "b" 2, "x" 7} {"a" 1, "b" 2, "x" 7, "y" 8})
  (assert-aggregate-sandbox-multiple {"x" 7, "y" 8, "z" 9} {"a" 1} {"a" 1, "x" 7, "y" 8, "z" 9}))

(defn- retrieve-sandbox
  [db-conn task-id]
  (let [datomic-db (d/db db-conn)]
    (-> (d/q '[:find ?s
               :in $ ?t
               :where
               [?e :instance/task-id ?t]
               [?e :instance/sandbox-directory ?s]]
             datomic-db task-id)
        ffirst)))

(deftest test-publish-sandbox-to-datomic!
  (let [db-conn (tu/restore-fresh-database! "datomic:mem://test-publish-sandbox-to-datomic!")]
    (testing "single entry"
      (let [num-tasks 20
            batch-size 4
            task-id->sandbox (pc/map-from-keys #(str "/sandbox/for/" %)
                                               (map #(str "exec-" %) (range num-tasks)))
            task-id->sandbox-in-db (pc/map-from-keys #(str "/sandbox/exists/" %)
                                                     (->> (keys task-id->sandbox)
                                                          (take 10)))
            task-id->sandbox-not-in-db (apply dissoc task-id->sandbox (keys task-id->sandbox-in-db))
            task-id->sandbox-agent (agent task-id->sandbox)]

        (doseq [[task-id _] task-id->sandbox-not-in-db]
          (tu/create-dummy-instance db-conn (tu/create-dummy-job db-conn) :task-id task-id))
        (doseq [[task-id sandbox] task-id->sandbox-in-db]
          (tu/create-dummy-instance db-conn (tu/create-dummy-job db-conn) :task-id task-id :sandbox-directory sandbox))

        (sandbox/publish-sandbox-to-datomic! db-conn batch-size task-id->sandbox-agent)
        (await task-id->sandbox-agent)

        (is (= num-tasks (-> task-id->sandbox-in-db (merge task-id->sandbox-not-in-db) count)))
        (doseq [[task-id sandbox] task-id->sandbox-not-in-db]
          (is (= sandbox (retrieve-sandbox db-conn task-id))))
        (doseq [[task-id sandbox] task-id->sandbox-in-db]
          (is (= sandbox (retrieve-sandbox db-conn task-id))))))))

(deftest test-start-sandbox-publisher
  (let [db-conn (tu/restore-fresh-database! "datomic:mem://test-start-sandbox-publisher")
        publish-batch-size 20
        publish-interval-ms 10
        num-publishes 4
        latch (CountDownLatch. num-publishes)
        task-id->sandbox-publish-history-atom (atom [])
        task-id->sandbox-state {:a 1, :b 2, :c 3}
        task-id->sandbox-agent (agent task-id->sandbox-state)]
    (with-redefs [sandbox/publish-sandbox-to-datomic!
                  (fn [datomic-conn batch-size task-id->sandbox-agent]
                    (is (= db-conn datomic-conn))
                    (is (= publish-batch-size batch-size))
                    (swap! task-id->sandbox-publish-history-atom conj @task-id->sandbox-agent)
                    (.countDown latch)
                    (send task-id->sandbox-agent sandbox/clear-agent-state @task-id->sandbox-agent)
                    (await task-id->sandbox-agent)
                    (Thread/sleep publish-interval-ms))]

      (let [cancel-fn (sandbox/start-sandbox-publisher
                        task-id->sandbox-agent db-conn publish-batch-size publish-interval-ms)]
        (.await latch 10 TimeUnit/SECONDS)
        (cancel-fn)

        (is (= {} @task-id->sandbox-agent))
        (is (<= num-publishes (count @task-id->sandbox-publish-history-atom) (inc num-publishes)))
        (is (= task-id->sandbox-state (first @task-id->sandbox-publish-history-atom)))
        (is (every? empty? (rest @task-id->sandbox-publish-history-atom)))))))

(deftest test-retrieve-sandbox-directories-on-agent
  (let [target-framework-id "framework-id-11"
        agent-hostname "www.mesos-agent-com"]
    (with-redefs [http/get (fn [url & [options]]
                             (is (= (str "http://" agent-hostname ":5051/state.json") url))
                             (is (= {:as :json-string-keys, :conn-timeout 5000, :socket-timeout 5000, :spnego-auth true} options))
                             {:body
                              {"completed_frameworks"
                               [{"completed_executors" [{"id" "executor-000", "directory" "/path/for/executor-000"}]
                                 "executors" [{"id" "executor-005", "directory" "/path/for/executor-005"}]
                                 "id" "framework-id-00"}
                                {"completed_executors" [{"id" "executor-010", "directory" "/path/for/executor-010"}]
                                 "executors" [{"id" "executor-015", "directory" "/path/for/executor-015"}]
                                 "id" "framework-id-01"}]
                               "frameworks"
                               [{"completed_executors" [{"id" "executor-101", "directory" "/path/for/executor-101"}
                                                        {"id" "executor-102", "directory" "/path/for/executor-102"}]
                                 "executors" [{"id" "executor-111", "directory" "/path/for/executor-111"}]
                                 "id" target-framework-id}
                                {"completed_executors" [{"id" "executor-201", "directory" "/path/for/executor-201"}]
                                 "executors" [{"id" "executor-211", "directory" "/path/for/executor-211"}]
                                 "id" "framework-id-12"}]}})]

      (testing "retrieve-sandbox-directories-on-agent"
        (let [actual-result (sandbox/retrieve-sandbox-directories-on-agent target-framework-id agent-hostname)
              expected-result {"executor-101" "/path/for/executor-101"
                               "executor-102" "/path/for/executor-102"
                               "executor-111" "/path/for/executor-111"}]
          (is (= expected-result actual-result)))))))

(deftest test-refresh-agent-cache-entry
  (let [framework-id "test-framework-id"
        mesos-agent-query-cache (atom (cache/fifo-cache-factory {} :threshold 2))
        pending-sync-initial-state {:framework-id framework-id, :pending-sync-hosts #{"host2.com"}}
        pending-sync-agent (agent pending-sync-initial-state)
        task-id->sandbox-agent (agent {})
        item-unavailable (future :unavailable)
        refresh-agent-cache-helper #(sandbox/refresh-agent-cache-entry
                                      framework-id mesos-agent-query-cache pending-sync-agent task-id->sandbox-agent %)]
    (with-redefs [sandbox/retrieve-sandbox-directories-on-agent
                  (fn [_ hostname]
                    (if (str/includes? hostname "badhost")
                      (throw (Exception. "Exception from test"))
                      {(str "task." hostname) (str "/path/to/" hostname "/sandbox")}))]

      (testing "cache population on good hosts"
        (refresh-agent-cache-helper "host1.com")
        (refresh-agent-cache-helper "host2.com")
        (await task-id->sandbox-agent)
        (await pending-sync-agent)
        (is (= :success @(cache/lookup @mesos-agent-query-cache "host1.com" item-unavailable)))
        (is (= :success @(cache/lookup @mesos-agent-query-cache "host2.com" item-unavailable)))
        (is (= :unavailable @(cache/lookup @mesos-agent-query-cache "host3.com" item-unavailable)))
        (is (= :unavailable @(cache/lookup @mesos-agent-query-cache "badhost.com" item-unavailable)))
        (is (= {"task.host1.com" "/path/to/host1.com/sandbox"
                "task.host2.com" "/path/to/host2.com/sandbox"}
               @task-id->sandbox-agent))
        (is (= (update pending-sync-initial-state :pending-sync-hosts
                       (fn [pending-sync-hosts] (-> pending-sync-hosts
                                                    (disj "host2.com"))))
               @pending-sync-agent)))

      (testing "cache eviction on good host"
        (refresh-agent-cache-helper "host3.com")
        (await task-id->sandbox-agent)
        (await pending-sync-agent)
        (is (= :unavailable @(cache/lookup @mesos-agent-query-cache "host1.com" item-unavailable)))
        (is (= :success @(cache/lookup @mesos-agent-query-cache "host2.com" item-unavailable)))
        (is (= :success @(cache/lookup @mesos-agent-query-cache "host3.com" item-unavailable)))
        (is (= :unavailable @(cache/lookup @mesos-agent-query-cache "badhost.com" item-unavailable)))
        (is (= {"task.host1.com" "/path/to/host1.com/sandbox"
                "task.host2.com" "/path/to/host2.com/sandbox"
                "task.host3.com" "/path/to/host3.com/sandbox"}
               @task-id->sandbox-agent))
        (is (= (update pending-sync-initial-state :pending-sync-hosts
                       (fn [pending-sync-hosts] (-> pending-sync-hosts
                                                    (disj "host2.com"))))
               @pending-sync-agent)))

      (testing "cache eviction on bad host"
        (refresh-agent-cache-helper "badhost.com")
        (await task-id->sandbox-agent)
        (await pending-sync-agent)
        (is (= :unavailable @(cache/lookup @mesos-agent-query-cache "host1.com" item-unavailable)))
        (is (= :unavailable @(cache/lookup @mesos-agent-query-cache "host2.com" item-unavailable)))
        (is (= :success @(cache/lookup @mesos-agent-query-cache "host3.com" item-unavailable)))
        (is (= :error @(cache/lookup @mesos-agent-query-cache "badhost.com" item-unavailable)))
        (is (= {"task.host1.com" "/path/to/host1.com/sandbox"
                "task.host2.com" "/path/to/host2.com/sandbox"
                "task.host3.com" "/path/to/host3.com/sandbox"}
               @task-id->sandbox-agent))
        (is (= (update pending-sync-initial-state :pending-sync-hosts
                       (fn [pending-sync-hosts] (-> pending-sync-hosts
                                                    (disj "host2.com")
                                                    (conj "badhost.com"))))
               @pending-sync-agent)))

      (testing "syncing of cached host"
        (refresh-agent-cache-helper "host3.com")
        (await task-id->sandbox-agent)
        (await pending-sync-agent)
        (is (= :unavailable @(cache/lookup @mesos-agent-query-cache "host1.com" item-unavailable)))
        (is (= :unavailable @(cache/lookup @mesos-agent-query-cache "host2.com" item-unavailable)))
        (is (= :success @(cache/lookup @mesos-agent-query-cache "host3.com" item-unavailable)))
        (is (= :error @(cache/lookup @mesos-agent-query-cache "badhost.com" item-unavailable)))
        (is (= {"task.host1.com" "/path/to/host1.com/sandbox"
                "task.host2.com" "/path/to/host2.com/sandbox"
                "task.host3.com" "/path/to/host3.com/sandbox"}
               @task-id->sandbox-agent))
        (is (= (update pending-sync-initial-state :pending-sync-hosts
                       (fn [pending-sync-hosts] (-> pending-sync-hosts
                                                    (disj "host2.com")
                                                    (conj "badhost.com" "host3.com"))))
               @pending-sync-agent))))))

(deftest test-start-host-sandbox-syncer
  (let [framework-id "test-framework-id"
        mesos-agent-query-cache (-> {"host1" (future :success)
                                     "host2" (future :success)
                                     "host3" (future :success)
                                     "host6" (future :error)}
                                    (cache/fifo-cache-factory :threshold 10)
                                    atom)
        pending-sync-initial-state {:framework-id framework-id
                                    :pending-sync-hosts #{"host1" "host2" "badhost3" "host4" "badhost5" "host6"}}
        pending-sync-agent (agent pending-sync-initial-state)
        task-id->sandbox-agent (agent {})
        sync-interval-ms 2
        sync-first-batch-latch (CountDownLatch. 2)
        total-items-synced-latch (CountDownLatch. 4)
        synced-hosts-atom (atom [])
        item-unavailable (future :unavailable)]
    (with-redefs [sandbox/retrieve-sandbox-directories-on-agent
                  (fn [_ hostname]
                    (.countDown sync-first-batch-latch)
                    (.countDown total-items-synced-latch)
                    (swap! synced-hosts-atom conj hostname)
                    (if (str/includes? hostname "badhost")
                      (throw (Exception. "Exception from test"))
                      {(str "task." hostname) (str "/path/to/" hostname "/sandbox")}))]
      (let [syncer-cancel-fn
            (sandbox/start-host-sandbox-syncer
              mesos-agent-query-cache pending-sync-agent task-id->sandbox-agent sync-interval-ms)]
        (try
          (.await sync-first-batch-latch 10 TimeUnit/SECONDS)
          (is (zero? (.getCount sync-first-batch-latch)))

          ;; evict items that will get populated during the second run of the syncer
          (reset! mesos-agent-query-cache
                  (-> @mesos-agent-query-cache
                      (cache/evict "host2")
                      (cache/evict "host3")))

          (.await total-items-synced-latch 10 TimeUnit/SECONDS)
          (await task-id->sandbox-agent)
          (await pending-sync-agent)

          (is (zero? (.getCount total-items-synced-latch)))
          (is (= :success @(cache/lookup @mesos-agent-query-cache "host1" item-unavailable)))
          (is (= :success @(cache/lookup @mesos-agent-query-cache "host2" item-unavailable)))
          (is (= :error @(cache/lookup @mesos-agent-query-cache "badhost3" item-unavailable)))
          (is (= :success @(cache/lookup @mesos-agent-query-cache "host4" item-unavailable)))
          (is (= :error @(cache/lookup @mesos-agent-query-cache "badhost5" item-unavailable)))
          (is (= :error @(cache/lookup @mesos-agent-query-cache "host6" item-unavailable)))
          (is (= #{"host1" "badhost3" "badhost5" "host6"}
                 (get @pending-sync-agent :pending-sync-hosts)))
          (is (= {"task.host2" "/path/to/host2/sandbox"
                  "task.host4" "/path/to/host4/sandbox"}
                 @task-id->sandbox-agent))
          (is (= #{"host2" "badhost3" "host4" "badhost5"}
                 (set @synced-hosts-atom)))

          (finally
            (syncer-cancel-fn)))))))

(deftest test-prepare-sandbox-publisher
  (with-redefs [sandbox/retrieve-sandbox-directories-on-agent
                (fn [_ hostname] {(str "task." hostname) (str "/path/to/" hostname "/sandbox")})]
    (let [db-conn (tu/restore-fresh-database! "datomic:mem://test-start-sandbox-publisher")
          publish-batch-size 20
          publish-interval-ms 10
          sync-interval-ms 10000
          framework-id "test-framework-id"
          mesos-agent-query-cache (atom (cache/fifo-cache-factory {} :threshold 2))
          {:keys [publisher-cancel-fn syncer-cancel-fn task-id->sandbox-agent] :as sandbox-state}
          (sandbox/prepare-sandbox-publisher framework-id db-conn publish-batch-size publish-interval-ms
                                             sync-interval-ms mesos-agent-query-cache)]

      (try
        (sandbox/update-sandbox sandbox-state {"sandbox-directory" "/path/to/sandbox", "task-id" "task-1", "type" "directory"})
        @(sandbox/sync-agent-sandboxes sandbox-state framework-id "host1")
        (await task-id->sandbox-agent)

        (is (= {"task-1" "/path/to/sandbox", "task.host1" "/path/to/host1/sandbox"}
               @task-id->sandbox-agent))
        (finally
          (publisher-cancel-fn)
          (syncer-cancel-fn))))))
