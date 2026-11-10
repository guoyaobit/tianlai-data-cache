#ifndef IXCAP_LIB_H
#define IXCAP_LIB_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef void ixcap_dev;
typedef void ixcap_pkt_buf;

/* Device control */
ixcap_dev* ixcap_init(const char* pci_addr, uint16_t rx_queues, uint16_t tx_queues);
void ixcap_set_promisc(ixcap_dev* ixcap, bool enabled);
const char* ixcap_get_pci_addr(ixcap_dev* ixcap);
uint32_t ixcap_get_link_speed(const ixcap_dev* ixcap);

/* TX operations */
void ixcap_send_batch_busy_wait(ixcap_dev* ixcap, uint16_t queue_id, ixcap_pkt_buf* bufs[], uint32_t num_bufs);
int32_t ixcap_tx_buf_alloc_batch(ixcap_dev* ixcap, uint16_t queue_id, ixcap_pkt_buf* bufs[], uint32_t num_bufs);

/* RX operations */
uint32_t ixcap_recv_batch(ixcap_dev* ixcap, uint16_t queue_id, ixcap_pkt_buf* bufs[], uint32_t num_bufs);

/* Packet operations */
void* ixcap_get_pkt_data(ixcap_pkt_buf* buf);
uint32_t ixcap_get_pkt_size(ixcap_pkt_buf* buf);
void ixcap_set_pkt_size(ixcap_pkt_buf* buf, uint32_t size);
void ixcap_free_pkt_buf(ixcap_pkt_buf* buf);

/* Device stats reading */
void ixcap_reset_stats(ixcap_dev* ixcap);
void ixcap_update_stats(ixcap_dev* ixcap);
uint32_t ixcap_get_rx_mbps(ixcap_dev* ixcap);
uint32_t ixcap_get_tx_mbps(ixcap_dev* ixcap);
double ixcap_get_rx_mpps(ixcap_dev* ixcap);
double ixcap_get_tx_mpps(ixcap_dev* ixcap);
size_t ixcap_get_rx_pkts(ixcap_dev* ixcap);
size_t ixcap_get_tx_pkts(ixcap_dev* ixcap);
size_t ixcap_get_rx_bytes(ixcap_dev* ixcap);
size_t ixcap_get_tx_bytes(ixcap_dev* ixcap);

/* Memory pool setting */
void ixcap_set_rx_entries(uint16_t rx_entries);
void ixcap_set_tx_entries(uint16_t tx_entries);
void ixcap_set_pkt_entry_size(uint16_t pkt_entry_size);
int32_t ixcap_get_rx_mempool_entries(ixcap_dev* ixcap, uint16_t queue_id);
int32_t ixcap_get_tx_mempool_entries(ixcap_dev* ixcap, uint16_t queue_id);

#ifdef __cplusplus
}
#endif

#endif
