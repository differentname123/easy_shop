// Detail page controller; the HTML remains a presentation-only Vue template.
(function (global) {
  'use strict';
  const shop = global.EasyShop = global.EasyShop || {};
  shop.createDetailController = function ({ ref, computed, api, session, browser }) {
    const goods = ref(null);
    const carouselRef = ref(null);
    const currentImgIndex = ref(0);
    const isLoggedIn = ref(false);
    const uid = ref('');
    const isDeepLoading = ref(true);
    const detailError = ref(false);
    let requested = null;
    let referralUid = '';
    let detailGeneration = 0;
    let identityRestore = null;
    let identitySnapshot = null;
    let verifiedSession = null;
    let converted = null;
    let pendingConversion = null;
    let conversionGeneration = 0;
    const carouselList = computed(() => {
      if (!goods.value) return [];
      if (Array.isArray(goods.value.carousel_images) && goods.value.carousel_images.length) return goods.value.carousel_images;
      return [goods.value.image_url || 'https://img.pddpic.com/mms-avatar/default_avatar.png'];
    });
    const detailGalleryList = computed(() => {
      if (!goods.value) return [];
      const list = Array.isArray(goods.value.carousel_images) && goods.value.carousel_images.length
        ? goods.value.carousel_images
        : (goods.value.image_url ? [goods.value.image_url] : []);
      return list.length > 1 ? list.slice(1) : list;
    });
    const realSpecs = computed(() => !goods.value || !Array.isArray(goods.value.specs) ? [] :
      goods.value.specs.filter(spec => spec && spec.label && spec.value));
    const onCarouselScroll = () => {
      if (!carouselRef.value) return;
      const width = carouselRef.value.clientWidth || global.innerWidth;
      currentImgIndex.value = Math.round(carouselRef.value.scrollLeft / width);
    };
    const goHome = () => { global.location.href = '/'; };
    const goToRegister = () => { global.location.href = '/?tab=user'; };
    const goBack = () => {
      if (global.history.length > 1) return global.history.back();
      let keyword = '';
      try { keyword = global.sessionStorage.getItem('easy_shop_home_search_keyword') || ''; } catch (_) {}
      global.location.href = keyword ? `/?keyword=${encodeURIComponent(keyword)}` : '/';
    };
    const restoreIdentity = () => {
      if (identityRestore && identitySnapshot && session.isCurrent(identitySnapshot)) return identityRestore;
      identitySnapshot = session.capture();
      identityRestore = session.restore(api).then(result => {
        if (result.user && session.isCurrent(result.session)) {
          verifiedSession = result.session;
          identitySnapshot = result.session;
          uid.value = result.user.uid;
          isLoggedIn.value = true;
        } else if (!result.stale) {
          verifiedSession = null;
          uid.value = '';
          isLoggedIn.value = false;
          if (result.missing) session.clear();
          identitySnapshot = session.capture();
        }
      }).catch(error => global.console.warn('获取用户信息失败:', error));
      return identityRestore;
    };
    const loadDeepDetail = async () => {
      if (!requested) return;
      const generation = ++detailGeneration;
      isDeepLoading.value = true;
      detailError.value = false;
      try {
        const params = new URLSearchParams({ ...requested, uid: uid.value || referralUid });
        const { ok, data } = await api.get(`/api/home/detail?${params}`);
        if (generation !== detailGeneration) return;
        if (ok && data.status === 'success' && data.data) {
          const detail = data.data;
          goods.value = {
            ...(goods.value || {}), ...detail,
            carousel_images: (detail.carousel_images && detail.carousel_images.length) ? detail.carousel_images : (goods.value?.carousel_images || []),
            specs: detail.specs || goods.value?.specs,
            service_guarantees: detail.service_guarantees || goods.value?.service_guarantees,
          };
        } else { detailError.value = true; }
      } catch (error) {
        if (generation === detailGeneration) {
          detailError.value = true;
          global.console.warn('获取商品深度详情失败，保留已有数据:', error);
        }
      } finally {
        if (generation === detailGeneration) isDeepLoading.value = false;
      }
    };
    const handleGenerateLink = async title => {
      if (!goods.value) return null;
      await restoreIdentity();
      const product = { ...goods.value };
      const snapshot = session.capture();
      // A token removed or changed in another tab must not retain the old UID.
      const accountUid = isLoggedIn.value && verifiedSession && session.isCurrent(verifiedSession) ? uid.value : '';
      const key = JSON.stringify([shop.goodsIdentity(product), accountUid, snapshot.token]);
      if (converted && converted.key === key && session.isCurrent(converted.session)) return converted.data;
      if (pendingConversion && pendingConversion.key === key && session.isCurrent(pendingConversion.session)) return pendingConversion.promise;
      const generation = ++conversionGeneration;
      browser.startSmartLoading(title || '正在为您生成专属口令与领券通道...', [
        '正在向拼多多官方申请专属券码与补贴...',
        '通道握手成功，正在生成拼多多APP识别口令...',
        '正在匹配最优短链接与唤起协议...', '福利口令已就绪，即将为您呈现...',
      ]);
      const promise = (async () => {
        try {
          const { ok, data } = await api.post('/api/promote/convert', shop.promotionPayload(product, accountUid));
          if (generation !== conversionGeneration || !session.isCurrent(snapshot)) return null;
          if (ok && data.status === 'success') {
            if (!data.is_fallback && data.final_link) converted = { key, data, session: snapshot };
            else if (data.is_fallback) browser.showToast('专属优惠暂不可用，已提供商品原链接');
            return data;
          }
          browser.showToast('专属优惠暂不可用，已提供商品原链接');
          return {
            final_link: product.product_url || '', is_fallback: true,
            command_text: `【好物精选】${product.name}\n【券后价】¥${product.price}\n${product.product_url || ''}`,
          };
        } catch (error) {
          global.console.warn('转链异常:', error);
          return null;
        } finally {
          if (generation === conversionGeneration) {
            pendingConversion = null;
            browser.stopSmartLoading();
          }
        }
      })();
      pendingConversion = { key, promise, session: snapshot };
      return promise;
    };
    const handleJumpToBuy = async () => {
      const info = await handleGenerateLink('正在打通拼多多领券通道...');
      if (!info) return browser.showToast('获取购买链接失败，请重试');
      const content = info.command_text || info.final_link;
      if (content) browser.copyText(content, '已复制抢购口令，正在前往拼多多领券...');
      browser.jumpToLink(info, goods.value?.product_url || '');
    };
    const handleCopyPromoteLink = async () => {
      const info = await handleGenerateLink('正在生成拼多多抢购口令...');
      if (!info) return browser.showToast('生成口令失败，请稍后重试');
      const content = info.command_text || info.final_link;
      if (content) browser.copyText(content, '抢购口令与领券链接已复制！打开拼多多APP即可自动识别，或通过链接直接下单~');
      else browser.showToast('暂无有效口令');
    };
    const initialize = async () => {
      const params = new URLSearchParams(global.location.search);
      requested = shop.goodsIdentity({ goods_id: params.get('goods_id'), goods_sign: params.get('goods_sign'), platform: params.get('platform') });
      referralUid = params.get('uid') || '';
      let hasSummary = false;
      try {
        const summary = JSON.parse(global.sessionStorage.getItem('current_goods_summary') || 'null');
        if (shop.matchesGoodsSummary(summary, requested)) {
          goods.value = summary;
          hasSummary = true;
        }
      } catch (_) {}
      if (!hasSummary) browser.startSmartLoading('正在搜寻专属优惠与正品补贴...', [
        '正在向拼多多官方申请隐藏优惠券...', '正在全网比价，核验到手最低价...',
        '正品货源与运费险核验中...', '马上为您呈现实时优惠与高清实拍...',
      ]);
      const identity = restoreIdentity();
      await loadDeepDetail();
      browser.stopSmartLoading();
      await identity;
      // Promotion conversion is deliberately triggered only by a user action.
    };
    const dispose = () => {
      detailGeneration += 1;
      conversionGeneration += 1;
      browser.dispose();
    };
    return {
      ...browser, goods, carouselRef, currentImgIndex, carouselList, detailGalleryList, realSpecs, isLoggedIn, uid,
      isDeepLoading, detailError, onCarouselScroll, goHome, goBack, goToRegister,
      retryLoadDeepDetail: loadDeepDetail, handleJumpToBuy, handleCopyPromoteLink, initialize, dispose,
      formatMoney: shop.formatMoney, formatSales: shop.formatSales,
    };
  };
  const { createApp, ref, computed, onMounted, onUnmounted } = global.Vue;
  createApp({
    setup() {
      const controller = shop.createDetailController({
        ref, computed, api: shop.createApi(), session: shop.createSessionStore(), browser: shop.createBrowserTools({ ref }),
      });
      onMounted(controller.initialize);
      onUnmounted(controller.dispose);
      return controller;
    },
  }).mount('#app');
})(globalThis);
