import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import {
  AlertTriangle,
  BellRing,
  Download,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";

import { Combobox } from "@/components/Combobox";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import {
  capacityOptions,
  colorOptions,
  familyOptions,
  productForSelection,
} from "@/lib/product-selection";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

import {
  changeLocale,
  connect,
  dismissUpdate,
  installUpdate,
  refreshProducts,
  saveSettings,
  setCategory,
  setIntervalSeconds,
  startWatching,
  stopWatching,
  testNotify,
  watcherStore,
} from "@/lib/store";
import {
  type Availability,
  type BarkAlertMode,
  type Category,
  describeAdvice,
  describeAvailability,
  formatTime,
  isUntrusted,
  type StatusTone,
  type Target,
  targetKey,
  type UserSettings,
} from "@/lib/types";

const ALERT_MODES: { value: BarkAlertMode; label: string }[] = [
  { value: "passive", label: "静默" },
  { value: "active", label: "普通" },
  { value: "timeSensitive", label: "时效性" },
  { value: "critical", label: "闹钟" },
];

const ALERT_MODE_HELP: Record<BarkAlertMode, string> = {
  passive: "只进入通知中心，不响铃、不亮屏。",
  active: "正常弹出并响铃。",
  timeSensitive: "可在允许时穿透专注模式。",
  critical: "alarm 最大音量持续响铃，需在 iPhone 允许 Bark 的重要警告。",
};

/** 四种展示状态各自的样式。 */
const TONE_CLASS: Record<StatusTone, string> = {
  inStock: "bg-in-stock/15 text-in-stock border-in-stock/30 font-medium",
  outOfStock: "bg-muted text-muted-foreground border-transparent",
  // 「未知」必须和「无货」长得完全不一样。这是整个项目的意义所在：上游把查询
  // 失败显示成「无货」，用户对着一个早已失效的程序空等了大半年。
  unknown: "bg-unknown/15 text-unknown border-unknown/40 font-medium",
  pending: "bg-transparent text-muted-foreground/60 border-dashed",
};

function StatusBadge({ availability }: { availability: Availability }) {
  const { label, tone, detail } = describeAvailability(availability);
  const badge = (
    <Badge variant="outline" className={`min-w-18 justify-center ${TONE_CLASS[tone]}`}>
      {label}
    </Badge>
  );
  if (!detail) return badge;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="cursor-help">{badge}</span>
      </TooltipTrigger>
      <TooltipContent className="max-w-90">{detail}</TooltipContent>
    </Tooltip>
  );
}

export default function App() {
  const ui = useSyncExternalStore(watcherStore.subscribe, watcherStore.getSnapshot);

  useEffect(() => {
    void connect();
    // 刻意不在清理函数里断开：这是应用级的单一连接，窗口活着它就该活着。
    // StrictMode 的重复调用由 connect 内部去重。
  }, []);

  const [storeNumber, setStoreNumber] = useState("");
  const [partNumber, setPartNumber] = useState("");
  const [productFamily, setProductFamily] = useState("");
  const [capacity, setCapacity] = useState("");
  const [color, setColor] = useState("");
  const [selectedUserId, setSelectedUserId] = useState("");
  const [userNameDraft, setUserNameDraft] = useState<string | null>(null);
  const [barkDraft, setBarkDraft] = useState<string | null>(null);
  const [intervalDraft, setIntervalDraft] = useState<number | null>(null);

  const activeUser =
    ui.settings.users.find((user) => user.id === selectedUserId) ?? ui.settings.users[0];
  const userNameValue = userNameDraft ?? activeUser?.name ?? "";
  // null 表示尚未编辑；空字符串是用户明确清空，不能退回已保存的地址。
  const barkValue = barkDraft ?? activeUser?.barkUrl ?? "";
  const intervalValue = intervalDraft ?? ui.settings.intervalSeconds;

  const storeOptions = useMemo(
    () => ui.stores.map((s) => ({ value: s.number, label: s.title })),
    [ui.stores],
  );
  // 只列当前品类。四个品类的型号加起来好几百条，全堆进一个下拉框，
  // 想找一台 Mac 得先划过所有 iPhone。
  const productOptions = useMemo(
    () =>
      ui.products
        .filter((p) => p.category === ui.category)
        .map((p) => ({ value: p.partNumber, label: p.title })),
    [ui.products, ui.category],
  );
  const iphoneFamilyOptions = useMemo(
    () => familyOptions(ui.products, ui.category),
    [ui.products, ui.category],
  );
  const iphoneCapacityOptions = useMemo(
    () => capacityOptions(ui.products, ui.category, productFamily),
    [ui.products, ui.category, productFamily],
  );
  const iphoneColorOptions = useMemo(
    () => colorOptions(ui.products, ui.category, productFamily, capacity),
    [ui.products, ui.category, productFamily, capacity],
  );

  function resetProductSelection() {
    setPartNumber("");
    setProductFamily("");
    setCapacity("");
    setColor("");
  }

  const targets = activeUser?.targets ?? [];
  const displayedRows = useMemo(() => {
    const byKey = new Map(ui.rows.map((row) => [targetKey(row.target), row]));
    return targets.map(
      (target) =>
        byKey.get(targetKey(target)) ?? {
          target,
          availability: { kind: "unknown", reason: "not_yet_checked" } as const,
          lastCheckedMs: null,
          consecutiveFailures: 0,
        },
    );
  }, [targets, ui.rows]);
  const totalTargets = useMemo(
    () => new Set(ui.settings.users.flatMap((user) => user.targets.map(targetKey))).size,
    [ui.settings.users],
  );

  const summary = useMemo(() => {
    let inStock = 0;
    let outOfStock = 0;
    let untrusted = 0;
    for (const r of displayedRows) {
      if (r.availability.kind === "in_stock") inStock += 1;
      else if (r.availability.kind === "out_of_stock") outOfStock += 1;
      if (isUntrusted(r.availability)) untrusted += 1;
    }
    return { inStock, outOfStock, untrusted };
  }, [displayedRows]);

  const canAdd = activeUser !== undefined && storeNumber !== "" && partNumber !== "";

  async function saveUser(patch: Partial<UserSettings>) {
    if (!activeUser) return;
    await saveSettings({
      ...ui.settings,
      users: ui.settings.users.map((user) =>
        user.id === activeUser.id ? { ...user, ...patch } : user,
      ),
    });
  }

  async function onAddUser() {
    const user: UserSettings = {
      id: crypto.randomUUID(),
      name: `用户 ${ui.settings.users.length + 1}`,
      barkUrl: "",
      alertMode: "active",
      targets: [],
    };
    setSelectedUserId(user.id);
    setUserNameDraft(null);
    setBarkDraft(null);
    await saveSettings({ ...ui.settings, users: [...ui.settings.users, user] });
  }

  async function onRemoveUser() {
    if (!activeUser || !window.confirm(`删除 ${activeUser.name} 及其全部监控目标？`)) return;
    const users = ui.settings.users.filter((user) => user.id !== activeUser.id);
    setSelectedUserId(users[0]?.id ?? "");
    setUserNameDraft(null);
    setBarkDraft(null);
    await saveSettings({ ...ui.settings, users });
  }

  async function onAdd() {
    if (!canAdd) return;
    const store = ui.stores.find((s) => s.number === storeNumber);
    const product = ui.products.find((p) => p.partNumber === partNumber);
    if (!store || !product) return;

    const next: Target = {
      locale: ui.settings.locale,
      storeNumber: store.number,
      storeTitle: store.title,
      partNumber: product.partNumber,
      productName: product.title,
      ...(product.companionPart ? { companionPart: product.companionPart } : {}),
    };
    if (targets.some((t) => targetKey(t) === targetKey(next))) return;
    await saveUser({ targets: [...targets, next] });
    resetProductSelection();
  }

  async function onRemove(t: Target) {
    await saveUser({ targets: targets.filter((x) => targetKey(x) !== targetKey(t)) });
  }

  return (
    <TooltipProvider delayDuration={200}>
      <div className="mx-auto flex h-screen max-w-6xl flex-col gap-4 p-6">
        <header className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Apple Pickup Watcher</h1>
            <p className="text-muted-foreground text-sm">
              一个账户一份监控清单，到货按各自的 Bark 方式提醒
            </p>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-muted-foreground text-sm">
              {ui.running ? "监听中" : "已暂停"}
            </span>
            {ui.running ? (
              <Button variant="secondary" onClick={() => void stopWatching()}>
                <Pause /> 暂停
              </Button>
            ) : (
              <Button onClick={() => void startWatching()} disabled={totalTargets === 0}>
                <Play /> 开始
              </Button>
            )}
          </div>
        </header>

        {ui.trouble !== null && (
          <Alert variant="destructive">
            <AlertTriangle />
            <AlertTitle>监控当前不可信</AlertTitle>
            <AlertDescription>
              {ui.trouble.reason}
              <span className="mt-1 block">
                此时列表里的状态不代表门店的真实库存，请先排查原因，不要干等。
              </span>
              {ui.trouble.advice !== null && (
                // 用户自己能做的那件事要单独拎出来。只说「被拦截了」而不说
                // 「换条网络试试」，用户只会盯着一个反复告警的窗口发呆 ——
                // issue #3 里那位就干等了三个小时。
                <span className="mt-1 block font-medium">
                  {describeAdvice(ui.trouble.advice)}
                </span>
              )}
            </AlertDescription>
          </Alert>
        )}

        {ui.update !== null && (
          // 只提示，不自作主张安装。用户可能正等着抢购，被强制重启是灾难。
          <Alert>
            <Download />
            <AlertTitle>有新版本 {ui.update.version}</AlertTitle>
            <AlertDescription>
              <span>当前版本 {ui.update.currentVersion}。安装后需重启应用生效。</span>
              <div className="mt-2 flex items-center gap-2">
                <Button
                  size="sm"
                  disabled={ui.installing}
                  onClick={() => void installUpdate()}
                >
                  {ui.installing ? "正在下载…" : "下载并安装"}
                </Button>
                <Button size="sm" variant="ghost" onClick={dismissUpdate}>
                  <X /> 稍后
                </Button>
              </div>
            </AlertDescription>
          </Alert>
        )}

        <section className="grid gap-3 rounded-lg border bg-muted/30 p-4 lg:grid-cols-[12rem_1fr_1.5fr_11rem_auto] lg:items-end">
          <div className="grid gap-1.5">
            <Label>监控账户</Label>
            <Combobox
              options={ui.settings.users.map((user) => ({ value: user.id, label: user.name }))}
              value={activeUser?.id ?? ""}
              onChange={(id) => {
                setSelectedUserId(id);
                setUserNameDraft(null);
                setBarkDraft(null);
              }}
              placeholder="先添加用户"
              searchPlaceholder="搜索用户…"
              emptyText="还没有用户"
            />
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="user-name">用户名称</Label>
            <Input
              id="user-name"
              value={userNameValue}
              placeholder="例如：马来西亚账户"
              disabled={!activeUser}
              onChange={(event) => setUserNameDraft(event.target.value)}
              onBlur={() => {
                setUserNameDraft(null);
                void saveUser({ name: userNameValue.trim() });
              }}
            />
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="bark">该用户的 Bark 地址</Label>
            <Input
              id="bark"
              className="select-text"
              placeholder="https://api.day.app/该用户的BarkKey"
              value={barkValue}
              disabled={!activeUser}
              onChange={(event) => setBarkDraft(event.target.value)}
              onBlur={() => {
                setBarkDraft(null);
                void saveUser({ barkUrl: barkValue.trim() });
              }}
            />
          </div>

          <div className="grid gap-1.5">
            <Label>提醒方式</Label>
            <Combobox
              options={ALERT_MODES}
              value={activeUser?.alertMode ?? ""}
              onChange={(mode) => void saveUser({ alertMode: mode as BarkAlertMode })}
              placeholder="选择提醒方式"
              searchPlaceholder="搜索方式…"
              emptyText="没有匹配方式"
              disabled={!activeUser}
            />
          </div>

          <div className="flex gap-2">
            <Button variant="secondary" onClick={() => void onAddUser()}>
              <Plus /> 添加用户
            </Button>
            <Button
              variant="ghost"
              size="icon"
              aria-label="删除当前用户"
              disabled={!activeUser}
              onClick={() => void onRemoveUser()}
            >
              <Trash2 />
            </Button>
          </div>

          {activeUser && (
            <div className="text-muted-foreground flex flex-wrap items-center gap-3 text-xs lg:col-span-5">
              <span>{ALERT_MODE_HELP[activeUser.alertMode]}</span>
              <Button
                variant="ghost"
                size="sm"
                disabled={!activeUser.barkUrl.trim()}
                onClick={() => void testNotify(activeUser.id)}
              >
                <BellRing /> 测试该用户提醒
              </Button>
            </div>
          )}
        </section>

        <section className="flex flex-wrap items-end gap-3">
          <div className="grid gap-1.5">
            <Label>地区</Label>
            <Combobox
              className="w-36"
              options={ui.regions.map((r) => ({ value: r.locale, label: r.title }))}
              value={ui.settings.locale}
              onChange={(locale) => {
                // 换地区后旧的门店和型号都不再适用，清掉待添加的选择。
                setStoreNumber("");
                resetProductSelection();
                void changeLocale(locale);
              }}
              placeholder="选择地区"
              searchPlaceholder="搜索地区…"
              emptyText="没有匹配的地区"
            />
          </div>

          <div className="grid gap-1.5">
            <Label>品类</Label>
            <Combobox
              className="w-36"
              options={ui.categories.map((c) => ({ value: c.value, label: c.title }))}
              value={ui.category}
              onChange={(value) => {
                // 换品类后旧的型号不再在下拉框里，清掉待添加的选择。门店不用清，
                // 它跟品类无关。
                resetProductSelection();
                setCategory(value as Category);
              }}
              placeholder="选择品类"
              searchPlaceholder="搜索品类…"
              emptyText="没有匹配的品类"
              disabled={ui.categories.length === 0}
            />
          </div>

          <div className="grid gap-1.5">
            <Label>门店</Label>
            <Combobox
              className="w-56"
              options={storeOptions}
              value={storeNumber}
              onChange={setStoreNumber}
              placeholder="选择自提门店"
              searchPlaceholder="搜索门店…"
              emptyText="没有匹配的门店"
              disabled={storeOptions.length === 0}
            />
          </div>

          {ui.category === "iphone" ? (
            <>
              <div className="grid gap-1.5">
                <Label>机型</Label>
                <Combobox
                  className="w-48"
                  options={iphoneFamilyOptions}
                  value={productFamily}
                  onChange={(value) => {
                    setProductFamily(value);
                    setCapacity("");
                    setColor("");
                    setPartNumber("");
                  }}
                  placeholder="选择机型"
                  searchPlaceholder="搜索机型…"
                  emptyText="没有匹配的机型"
                  disabled={iphoneFamilyOptions.length === 0}
                />
              </div>

              <div className="grid gap-1.5">
                <Label>容量（存储）</Label>
                <Combobox
                  className="w-36"
                  options={iphoneCapacityOptions}
                  value={capacity}
                  onChange={(value) => {
                    setCapacity(value);
                    setColor("");
                    setPartNumber("");
                  }}
                  placeholder="选择容量"
                  searchPlaceholder="搜索容量…"
                  emptyText="没有匹配的容量"
                  disabled={productFamily === "" || iphoneCapacityOptions.length === 0}
                />
              </div>

              <div className="grid gap-1.5">
                <Label>颜色</Label>
                <Combobox
                  className="w-40"
                  options={iphoneColorOptions}
                  value={color}
                  onChange={(value) => {
                    setColor(value);
                    const product = productForSelection(
                      ui.products,
                      ui.category,
                      productFamily,
                      capacity,
                      value,
                    );
                    setPartNumber(product?.partNumber ?? "");
                  }}
                  placeholder="选择颜色"
                  searchPlaceholder="搜索颜色…"
                  emptyText="没有匹配的颜色"
                  disabled={capacity === "" || iphoneColorOptions.length === 0}
                />
              </div>
            </>
          ) : (
            <div className="grid gap-1.5">
              <Label>型号</Label>
              <Combobox
                className="w-80"
                options={productOptions}
                value={partNumber}
                onChange={setPartNumber}
                placeholder="选择型号"
                searchPlaceholder="搜索型号…"
                emptyText="没有匹配的型号"
                disabled={productOptions.length === 0}
              />
            </div>
          )}

          <Button variant="secondary" onClick={() => void onAdd()} disabled={!canAdd}>
            <Plus /> 添加
          </Button>

          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                aria-label="从 Apple 官网更新当前品类的型号列表"
                disabled={ui.refreshing}
                onClick={() => void refreshProducts()}
              >
                <RefreshCw className={ui.refreshing ? "animate-spin" : undefined} />
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              从 Apple 官网更新当前品类的型号列表。新机发布后用这个，不必等程序更新。
            </TooltipContent>
          </Tooltip>
        </section>

        <section className="flex flex-wrap items-end gap-4">
          <div className="grid gap-1.5">
            <Label htmlFor="interval">查询间隔（秒）</Label>
            <Input
              id="interval"
              type="number"
              min={5}
              className="w-28 select-text"
              value={intervalValue}
              onChange={(e) => setIntervalDraft(e.target.valueAsNumber)}
              onBlur={() => {
                const s = Number.isFinite(intervalValue) ? Math.round(intervalValue) : 30;
                setIntervalDraft(null);
                void setIntervalSeconds(s);
              }}
            />
          </div>

          <div className="flex items-center gap-2 pb-2">
            <Switch
              id="sound"
              checked={ui.settings.soundEnabled}
              onCheckedChange={(v) =>
                void saveSettings({ ...ui.settings, soundEnabled: v })
              }
            />
            <Label htmlFor="sound">提示音</Label>
          </div>

          <div className="flex items-center gap-2 pb-2">
            <Switch
              id="openbag"
              checked={ui.settings.openBagOnHit}
              onCheckedChange={(v) =>
                void saveSettings({ ...ui.settings, openBagOnHit: v })
              }
            />
            <Label htmlFor="openbag">有货时打开购物袋</Label>
          </div>

        </section>

        <Separator />

        <section className="min-h-0 flex-1 overflow-hidden rounded-lg border">
          <ScrollArea className="h-full">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-24">状态</TableHead>
                  <TableHead>门店</TableHead>
                  <TableHead>型号</TableHead>
                  <TableHead className="w-24">最后检查</TableHead>
                  <TableHead className="w-12" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {displayedRows.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={5} className="text-muted-foreground h-24 text-center">
                      {ui.ready
                        ? activeUser
                          ? "该用户还没有监控目标。选好门店和型号后点「添加」。"
                          : "先添加一个监控账户。"
                        : "正在载入…"}
                    </TableCell>
                  </TableRow>
                ) : (
                  displayedRows.map((row) => (
                    <TableRow key={targetKey(row.target)}>
                      <TableCell>
                        <StatusBadge availability={row.availability} />
                      </TableCell>
                      <TableCell className="font-medium">{row.target.storeTitle}</TableCell>
                      <TableCell className="text-muted-foreground">
                        {row.target.productName}
                      </TableCell>
                      <TableCell className="text-muted-foreground tabular-nums">
                        {formatTime(row.lastCheckedMs)}
                      </TableCell>
                      <TableCell>
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label="删除这条监控"
                          onClick={() => void onRemove(row.target)}
                        >
                          <Trash2 />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </ScrollArea>
        </section>

        <footer className="text-muted-foreground text-sm">
          账户 {ui.settings.users.length} 个 · {activeUser?.name ?? "未选用户"} 监控{" "}
          {displayedRows.length} 项 · 有货 {summary.inStock} · 无货 {summary.outOfStock}
          {summary.untrusted > 0 && (
            // 把「其中多少项查不到」单独点出来：这个数字大于 0 时，
            // 界面上那些「无货」也未必反映真实情况。
            <span className="text-unknown"> · 查不到 {summary.untrusted}</span>
          )}
          {ui.running && ui.pacing !== null && ui.pacing.paced && (
            // 请求预算在拉长间隔时明说，否则用户会以为设的 30 秒没生效。
            <span> · 受 Apple 频率限制，下一轮 {ui.pacing.nextCheckInSecs} 秒后</span>
          )}
        </footer>

        <section className="h-36 shrink-0 overflow-hidden rounded-lg border">
          <ScrollArea className="h-full p-3">
            <pre className="text-muted-foreground font-mono text-xs leading-5 whitespace-pre-wrap select-text">
              {ui.logs.length === 0 ? "日志会显示在这里。" : ui.logs.join("\n")}
            </pre>
          </ScrollArea>
        </section>
      </div>
    </TooltipProvider>
  );
}
