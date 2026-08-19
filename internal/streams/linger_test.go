package streams

import (
	"net/url"
	"testing"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/core"
	"github.com/AlexxIT/go2rtc/pkg/probe"
	"github.com/stretchr/testify/require"
)

type lingerTestProducer struct {
	core.Connection
	stopStarted chan struct{}
	allowStop   <-chan struct{}
}

func (p *lingerTestProducer) Start() error {
	return nil
}

func (p *lingerTestProducer) Stop() error {
	if p.stopStarted != nil {
		close(p.stopStarted)
	}
	if p.allowStop != nil {
		<-p.allowStop
	}
	return p.Connection.Stop()
}

func producerConnected(p *Producer) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.conn != nil
}

func newLingerTestStream(t *testing.T, linger time.Duration) (*Stream, *Producer, core.Consumer) {
	t.Helper()

	codec := &core.Codec{Name: core.CodecH264}
	media := &core.Media{
		Kind:      core.KindVideo,
		Direction: core.DirectionRecvonly,
		Codecs:    []*core.Codec{codec},
	}
	receiver := core.NewReceiver(media, codec)
	connection := &lingerTestProducer{Connection: core.Connection{
		Medias:    []*core.Media{media},
		Receivers: []*core.Receiver{receiver},
	}}
	producer := &Producer{
		url:       "test:source",
		conn:      connection,
		state:     stateStart,
		receivers: []*core.Receiver{receiver},
	}
	consumer := probe.Create("initial", url.Values{"video": {"h264"}})
	stream := &Stream{
		producers: []*Producer{producer},
		consumers: []core.Consumer{consumer},
		linger:    linger,
	}
	return stream, producer, consumer
}

func TestLingerRetainsProducerUntilDeadline(t *testing.T) {
	stream, producer, consumer := newLingerTestStream(t, 30*time.Millisecond)

	stream.RemoveConsumer(consumer)
	require.True(t, producerConnected(producer))
	require.Eventually(t, func() bool {
		return !producerConnected(producer)
	}, time.Second, 5*time.Millisecond)
}

func TestLingerIsCancelledByNewConsumer(t *testing.T) {
	stream, producer, consumer := newLingerTestStream(t, 100*time.Millisecond)
	stream.RemoveConsumer(consumer)
	require.True(t, producerConnected(producer))
	require.Len(t, producer.GetMedias(), 1)

	replacement := probe.Create("replacement", url.Values{"video": {"h264"}})
	require.Len(t, replacement.GetMedias(), 1)
	producerCodec, consumerCodec := producer.GetMedias()[0].MatchMedia(replacement.GetMedias()[0])
	require.NotNil(t, producerCodec)
	require.NotNil(t, consumerCodec)
	require.NoError(t, stream.AddConsumer(replacement))

	stream.mu.Lock()
	require.Nil(t, stream.stopTimer)
	stream.mu.Unlock()
	time.Sleep(150 * time.Millisecond)
	require.True(t, producerConnected(producer))

	stream.SetLinger(0)
	stream.RemoveConsumer(replacement)
}

func TestFailedConsumerRestoresLingerWindow(t *testing.T) {
	stream, producer, consumer := newLingerTestStream(t, 100*time.Millisecond)
	stream.RemoveConsumer(consumer)
	require.True(t, producerConnected(producer))

	incompatible := probe.Create("incompatible", url.Values{"video": {"h265"}})
	require.Error(t, stream.AddConsumer(incompatible))
	require.True(t, producerConnected(producer))

	stream.mu.Lock()
	require.NotNil(t, stream.stopTimer)
	stream.mu.Unlock()
	require.Eventually(t, func() bool {
		return !producerConnected(producer)
	}, time.Second, 5*time.Millisecond)
}

func TestLingerExpiryCommitsBeforeNewConsumerAdmission(t *testing.T) {
	stream, producer, consumer := newLingerTestStream(t, time.Millisecond)
	connection := producer.conn.(*lingerTestProducer)
	connection.stopStarted = make(chan struct{})
	allowStop := make(chan struct{})
	connection.allowStop = allowStop

	stream.RemoveConsumer(consumer)
	select {
	case <-connection.stopStarted:
	case <-time.After(time.Second):
		t.Fatal("linger expiry did not start stopping the producer")
	}

	if stream.mu.TryLock() {
		stream.mu.Unlock()
		close(allowStop)
		t.Fatal("linger expiry released the stream before producer stop committed")
	}

	admitted := make(chan int32, 1)
	go func() {
		admitted <- stream.beginConsumerAdd()
	}()

	close(allowStop)
	select {
	case consN := <-admitted:
		require.Equal(t, int32(0), consN)
	case <-time.After(time.Second):
		t.Fatal("consumer admission did not resume after producer stop")
	}
	require.False(t, producerConnected(producer))
	require.Equal(t, int32(1), stream.pending.Load())
	stream.pending.Add(-1)
}
